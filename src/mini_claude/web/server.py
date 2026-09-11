from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from aiohttp import WSMsgType, web

from mini_claude.core.bus.envelope import HandlerError
from mini_claude.core.bus.workspace_commands import (
    WorkspaceChangedEvent,
    WorkspaceProjectsChangedEvent,
)
from mini_claude.core.config import MiniConfig
from mini_claude.web.desktop_services import DesktopServices
from mini_claude.web.workspace import Workspace

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("static")
MAX_COMMAND_BYTES = 16 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024 * 1024
CONFIG = web.AppKey("config", MiniConfig)
PROJECT_PATH = web.AppKey("project_path", Path)
WEBSOCKETS = web.AppKey("websockets", set[web.WebSocketResponse])
WEBSOCKET_PROJECTS = web.AppKey("websocket_projects", dict[web.WebSocketResponse, Path])
PENDING_RUNS = web.AppKey("pending_runs", dict[web.WebSocketResponse, set[str]])
WORKSPACE = web.AppKey("workspace", Workspace)
SERVICES = web.AppKey("desktop_services", DesktopServices)


# 校验实际监听端口与本机 Host，WebSocket 额外要求显式同源 Origin。
@web.middleware
async def local_only(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    address = request.transport.get_extra_info("sockname") if request.transport else None
    port = address[1] if address else 7438
    suffix = "" if port == 80 else f":{port}"
    allowed_hosts = {f"127.0.0.1{suffix}", f"localhost{suffix}", f"[::1]{suffix}"}
    if request.host not in allowed_hosts:
        raise web.HTTPForbidden(text="Only local hosts are allowed")
    origin = request.headers.get("Origin")
    if origin is not None or request.path == "/ws":
        if origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="A same-origin request is required")
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["X-Frame-Options"] = "DENY"
    return response


# 首页与资源仅允许读取固定静态目录内的普通文件，禁止目录与软链接逃逸。
async def static_file(request: web.Request) -> web.FileResponse:
    name = request.match_info.get("filename", "index.html")
    if name.startswith(".") or "/" in name or "\\" in name:
        raise web.HTTPNotFound()
    root = STATIC_DIR.resolve()
    target = (root / name).resolve()
    if target.parent != root or not target.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(target, headers={"Cache-Control": "no-cache"})


# 只公开界面需要的项目与连接信息，避免序列化包含凭据的完整配置。
async def project_info(request: web.Request) -> web.Response:
    return web.json_response(request.app[WORKSPACE].info())


# 每个连接只允许操作建立连接时的项目，切换期间拒绝尚未关闭的旧项目连接。
def require_socket_project(ws: web.WebSocketResponse, app: web.Application) -> None:
    workspace = app[WORKSPACE]
    workspace.require_project()
    selected = app[WEBSOCKET_PROJECTS].get(ws)
    if selected != workspace.project_path or not workspace.has_project(selected):
        raise ValueError("项目已切换或正在移除，请等待界面重新连接。")


# 每个浏览器帧只能包含一个 JSON 对象，验证后保持原始 JSON 文本转发到 core。
async def browser_to_core(
    ws: web.WebSocketResponse, writer: asyncio.StreamWriter, app: web.Application,
) -> None:
    async for message in ws:
        if message.type == WSMsgType.TEXT:
            try:
                if "\n" in message.data or "\r" in message.data:
                    raise ValueError("Multiline command")
                command = json.loads(message.data)
                if not isinstance(command, dict):
                    raise ValueError("Command must be an object")
            except (ValueError, RecursionError):
                await ws.close(code=1007, message=b"Expected one JSON object per frame")
                return
            if await desktop_command(command, ws, app):
                continue
            method = command.get("method")
            if method not in {"core.ping", "event.subscribe"}:
                try:
                    require_socket_project(ws, app)
                except ValueError as exc:
                    await ws.send_json({"jsonrpc": "2.0", "id": command.get("id"),
                                        "error": {"code": -32000, "message": str(exc)}})
                    continue
            if method in {"session.send_message", "session.create", "session.compact", "agent.run"}:
                app[PENDING_RUNS][ws].add(json.dumps(command.get("id")))
            writer.write(message.data.encode("utf-8") + b"\n")
            await writer.drain()
        elif message.type == WSMsgType.BINARY:
            await ws.close(code=1003, message=b"Text JSON frames only")
            return


# 桌面命令与 core 使用同一事件连接，切换成功的响应必须先于连接重建通知。
async def desktop_command(
    command: dict[str, object], ws: web.WebSocketResponse, app: web.Application,
) -> bool:
    method = command.get("method")
    workspace = app[WORKSPACE]
    services = app[SERVICES]
    if not isinstance(method, str) or not (workspace.handles(method) or services.handles(method)):
        return False
    before = (workspace.project_path, workspace.project_selected)
    try:
        if method not in {
            "workspace.list", "workspace.sessions", "workspace.pick", "workspace.select",
            "workspace.remove",
        }:
            require_socket_project(ws, app)
        params = command.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("命令参数必须是对象。")
        handler = workspace if workspace.handles(method) else services
        service_params = params if handler is workspace else {
            key: value for key, value in params.items() if key != "type"
        }
        result = await handler.handle(method, service_params)
        await ws.send_json({"jsonrpc": "2.0", "id": command.get("id"), "result": result})
    except (ValueError, RuntimeError, OSError, HandlerError) as exc:
        await ws.send_json({"jsonrpc": "2.0", "id": command.get("id"),
                            "error": {"code": exc.code if isinstance(exc, HandlerError) else -32000,
                                      "message": str(exc)}})
        return True
    connections: set[web.WebSocketResponse] = app[WEBSOCKETS]
    if method == "workspace.remove":
        changed = {"kind": "event", "event": WorkspaceProjectsChangedEvent(
            **workspace.listing(),
        ).model_dump()}
        for connection in list(connections):
            if not connection.closed:
                await connection.send_json(changed)
    if before != (workspace.project_path, workspace.project_selected):
        event = {"kind": "event", "event": WorkspaceChangedEvent(**workspace.info()).model_dump()}
        for connection in list(connections):
            if not connection.closed:
                await connection.send_json(event)
                await connection.close(code=1012, message=b"Workspace changed")
    return True


# core 的响应与事件共享同一 NDJSON 流，逐行去除分隔符后直接发送给浏览器。
async def core_to_browser(
    reader: asyncio.StreamReader, ws: web.WebSocketResponse, app: web.Application,
) -> None:
    while line := await reader.readline():
        response = json.loads(line)
        if isinstance(response, dict) and "id" in response and (
            "result" in response or "error" in response
        ):
            app[PENDING_RUNS].get(ws, set()).discard(json.dumps(response["id"]))
        await ws.send_str(line.rstrip(b"\r\n").decode("utf-8"))
    await ws.close(code=1011, message=b"mini-core disconnected")


# 为每个浏览器连接分配独立 TCP 连接，同时运行两个方向的转发并对称清理资源。
async def websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_COMMAND_BYTES)
    await ws.prepare(request)
    request.app[WEBSOCKETS].add(ws)
    request.app[WEBSOCKET_PROJECTS][ws] = request.app[WORKSPACE].project_path
    request.app[PENDING_RUNS][ws] = set()
    writer: asyncio.StreamWriter | None = None
    tasks: list[asyncio.Task[None]] = []
    config = request.app[WORKSPACE].config
    try:
        try:
            reader, core_writer = await asyncio.wait_for(
                asyncio.open_connection(config.host, config.port, limit=MAX_EVENT_BYTES), timeout=3,
            )
            writer = core_writer
        except (OSError, TimeoutError):
            await ws.close(code=1013, message=b"mini-core unavailable; run mini-core")
            return ws
        tasks = [
            asyncio.create_task(browser_to_core(ws, core_writer, request.app)),
            asyncio.create_task(core_to_browser(reader, ws, request.app)),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (OSError, ValueError) as exc:
        logger.warning("WebSocket bridge closed: %s", exc)
        await ws.close(code=1011, message=b"mini-core stream interrupted")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if writer is not None:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        request.app[WEBSOCKETS].discard(ws)
        request.app[WEBSOCKET_PROJECTS].pop(ws, None)
        request.app[PENDING_RUNS].pop(ws, None)
        await ws.close()
    return ws


# 服务退出时主动关闭浏览器连接，让桥接任务及时释放 core 连接与订阅。
async def close_websockets(app: web.Application) -> None:
    websockets: set[web.WebSocketResponse] = app[WEBSOCKETS]
    await asyncio.gather(*(
        ws.close(code=1001, message=b"mini-web shutting down") for ws in list(websockets)
    ))


# 计划调度与桌面网关使用同一生命周期，不在应用退出后留下后台执行器。
async def start_services(app: web.Application) -> None:
    await app[SERVICES].start()


# 关闭计划调度并保存未确认状态，避免重启后重复提交任务。
async def stop_services(app: web.Application) -> None:
    await app[SERVICES].stop()


# 构建可独立启动和测试的 HTTP 应用，直到浏览器订阅时才连接 core。
def create_app(
    config: MiniConfig, project_path: Path | None = None, *, workspace: Workspace | None = None,
) -> web.Application:
    app = web.Application(middlewares=[local_only], client_max_size=MAX_COMMAND_BYTES)
    app[CONFIG] = config
    app[PROJECT_PATH] = (project_path or Path.cwd()).resolve()
    app[WORKSPACE] = workspace or Workspace(config, app[PROJECT_PATH])
    app[WEBSOCKETS] = set()
    app[WEBSOCKET_PROJECTS] = {}
    app[PENDING_RUNS] = {}

    # 计划变更只推送至对应项目的连接，后台其他项目不会污染当前任务列表。
    async def schedule_changed(payload: dict[str, object]) -> None:
        connections: dict[web.WebSocketResponse, Path] = app[WEBSOCKET_PROJECTS]
        for ws, project in list(connections.items()):
            if not ws.closed and str(project) == payload.get("project_path"):
                try:
                    await ws.send_json({"kind": "event", "event": payload})
                except (OSError, RuntimeError):
                    logger.debug("计划通知发送时客户端已断开")

    app[SERVICES] = DesktopServices(app[WORKSPACE], on_change=schedule_changed)

    # 在项目列表提交前拒绝尚未确认的任务，再解除该项目的计划调度所有权。
    async def before_remove(project: Path) -> None:
        if any(app[PENDING_RUNS].get(ws) for ws, selected in app[WEBSOCKET_PROJECTS].items()
               if selected == project):
            raise ValueError("项目中有正在运行或提交的任务，请先停止任务再移除。")
        await app[SERVICES].detach_project(project)

    app[WORKSPACE].before_remove = before_remove
    app.router.add_get("/", static_file)
    app.router.add_get("/assets/{filename}", static_file)
    app.router.add_get("/api/info", project_info)
    app.router.add_get("/ws", websocket)
    app.on_shutdown.append(close_websockets)
    app.on_startup.append(start_services)
    app.on_shutdown.append(stop_services)
    return app
