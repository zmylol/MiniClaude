"""浏览器 E2E 专用：真实事件传输配合内存业务，不调用模型或读写实际项目文件。

运行：.venv/bin/python tests/web/fixture_core.py
页面：http://127.0.0.1:7440；关闭进程后所有测试会话自动丢弃。
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from mini_claude.core.bus import commands, events
from mini_claude.core.bus.desktop_commands import PluginsResult
from mini_claude.core.bus.envelope import INVALID_PARAMS, HandlerError
from mini_claude.core.config import MiniConfig
from mini_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from mini_claude.core.transport.socket_server import SocketServer, get_connection_writer
from mini_claude.web.server import create_app
from mini_claude.web.workspace import Workspace


# 功能：生成与真实事件一致的 UTC 时间戳。
# 设计：仅供事件元数据使用，不读取项目或用户配置。
def now() -> str:
    return datetime.now(UTC).isoformat()


# 功能：组合真实 TCP / WebSocket 传输与不调用模型的内存会话。
# 设计：所有业务结果均在此文件内生成，core 绑定 OS 随机分配的本机端口。
async def create_fixture_app() -> web.Application:
    broadcaster = IpcEventBroadcaster()
    core = SocketServer("127.0.0.1", 0, broadcaster=broadcaster)
    cores = [core]
    project_ports: dict[int, tuple[Path, IpcEventBroadcaster]] = {}
    sessions: dict[str, list[dict[str, Any]]] = {}
    metadata: dict[str, commands.SessionSummary] = {}
    session_tasks: dict[str, asyncio.Task[Any]] = {}
    temporary = tempfile.TemporaryDirectory(prefix="miniclaude-desktop-e2e-")
    project_path = Path(temporary.name).resolve()
    permissions: dict[str, tuple[str, asyncio.Future[str]]] = {}
    running: set[asyncio.Task[Any]] = set()

    # 功能：根据当前 TCP 连接识别项目和事件总线。
    # 设计：三个真实端口隔离历史与事件，跨项目切换不能共享同一个模拟后端。
    def request_project() -> tuple[Path, IpcEventBroadcaster]:
        return project_ports[get_connection_writer().get_extra_info("sockname")[1]]

    # 功能：返回测试 core 的连通性信息。
    # 设计：沿用真实 PongResult 格式，版本名明确标记为 fixture。
    async def ping(params: dict[str, Any]) -> commands.PongResult:
        return commands.PongResult(
            server_version="e2e-fixture", uptime_ms=0,
            received_at=now(), project_path=str(request_project()[0]),
        )

    # 功能：把浏览器订阅绑定到当前真实 TCP 连接。
    # 设计：由既有 Broadcaster 完成事件包封装、主题匹配与断开清理。
    async def subscribe(params: dict[str, Any]) -> commands.EventSubscribeResult:
        command = commands.EventSubscribeCommand.model_validate(params)
        subscription_id = request_project()[1].subscribe(
            get_connection_writer(), command.topics, command.scope,
        )
        return commands.EventSubscribeResult(subscription_id=subscription_id)

    # 功能：创建只存在于本进程内的测试会话。
    # 设计：生成独立 session_id，并通过真实广播通道发送 session.created。
    async def create_session(params: dict[str, Any]) -> commands.SessionCreateResult:
        command = commands.SessionCreateCommand.model_validate(params)
        session_id = f"sess-{uuid.uuid4().hex[:12]}"
        sessions[session_id] = []
        metadata[session_id] = commands.SessionSummary(
            session_id=session_id, title=command.title, mode=command.mode,
            status="waiting_for_input", model=command.model or "fixture-model",
            permission_mode=command.permission_mode, project_path=str(request_project()[0]),
            created_at=now(), updated_at=now(), run_ids=[],
        )
        await request_project()[1].handle(events.SessionCreatedEvent(
            session_id=session_id, mode=command.mode, ts=now(),
        ))
        return commands.SessionCreateResult(session_id=session_id, status="waiting_for_input")

    # 功能：列出内存会话并暴露当前运行状态，供界面重载恢复。
    # 设计：使用生产摘要类型验证前端与 Core 的实际契约。
    async def list_sessions(params: dict[str, Any]) -> commands.SessionListResult:
        path = str(request_project()[0])
        return commands.SessionListResult(
            sessions=[item for item in metadata.values() if item.project_path == path],
            project_path=path,
        )

    # 功能：在内存中应用会话名称与模型权限设置。
    # 设计：让浏览器通过真实 RPC 验证按钮效果，不更改用户配置。
    async def configure(params: dict[str, Any]) -> commands.SessionUpdateResult:
        item = metadata[params["session_id"]]
        for key in ("title", "model", "permission_mode"):
            if key in params:
                setattr(item, key, params[key])
        return commands.SessionUpdateResult(session=item)

    # 功能：取消真实挂起任务并等待取消事件传播完成。
    # 设计：覆盖浏览器发送长请求期间仍可停止和继续的行为。
    async def cancel(params: dict[str, Any]) -> commands.SessionCancelResult:
        task = session_tasks.get(params["session_id"])
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return commands.SessionCancelResult(cancelled=True)
        return commands.SessionCancelResult(cancelled=False)

    # 功能：删除 fixture 会话并防止重载后再次出现。
    # 设计：先取消运行，再删除两份内存索引。
    async def delete(params: dict[str, Any]) -> commands.SessionDeleteResult:
        await cancel(params)
        metadata.pop(params["session_id"])
        sessions.pop(params["session_id"])
        return commands.SessionDeleteResult()

    # 功能：提供可切换的测试模型名称。
    # 设计：所有模型仍使用本文件的固定输出，不连接供应商。
    async def models(params: dict[str, Any]) -> commands.ConfigModelsResult:
        return commands.ConfigModelsResult(current_model="fixture-model", models=[
            commands.ModelOption(id="fixture-model", label="Fixture Model"),
            commands.ModelOption(id="fixture-fast", label="Fixture Fast"),
        ])

    # 功能：返回空的插件列表用于真实空状态界面。
    # 设计：插件服务本身另有独立进程测试，这里不启动外部软件。
    async def plugins(params: dict[str, Any]) -> PluginsResult:
        return PluginsResult(servers=[])

    # 功能：返回内存中累积的用户与助手消息，支持同一会话的连续交互。
    # 设计：拒绝不存在的会话，不尝试恢复或读取本地历史文件。
    async def history(params: dict[str, Any]) -> commands.SessionGetHistoryResult:
        command = commands.SessionGetHistoryCommand.model_validate(params)
        if command.session_id not in sessions:
            raise HandlerError(INVALID_PARAMS, "Unknown fixture session")
        return commands.SessionGetHistoryResult(messages=sessions[command.session_id])

    # 功能：以可见的逐字间隔模拟模型输出。
    # 设计：每个字符发布真实 LlmTokenEvent，固定 40 ms 延迟方便浏览器观察流式行为。
    async def stream(run_id: str, content: str) -> None:
        for token in content:
            await request_project()[1].handle(events.LlmTokenEvent(run_id=run_id, token=token, ts=now()))
            await asyncio.sleep(0.04)

    # 功能：通过独立 JSON-RPC 请求批准或拒绝挂起的虚拟工具调用。
    # 设计：先广播决策，再解除发送任务的等待；测试决策不会写入真实权限策略。
    async def respond(params: dict[str, Any]) -> commands.PermissionRespondResult:
        command = commands.PermissionRespondCommand.model_validate(params)
        pending = permissions.get(command.tool_use_id)
        if pending is None or pending[1].done():
            raise HandlerError(INVALID_PARAMS, "No pending fixture permission")
        if command.decision not in {"allow_once", "always_allow", "deny_once", "always_deny"}:
            raise HandlerError(INVALID_PARAMS, "Unknown permission decision")
        run_id, decision = pending
        event_type = (
            events.PermissionGrantedEvent if command.decision in {"allow_once", "always_allow"}
            else events.PermissionDeniedEvent
        )
        await request_project()[1].handle(event_type(
            run_id=run_id, tool_use_id=command.tool_use_id, decision=command.decision, ts=now(),
        ))
        decision.set_result(command.decision)
        return commands.PermissionRespondResult()

    # 功能：模拟流式回答、虚拟 read_file、审批和运行完成的完整事件顺序。
    # 设计：README 输出是常量；审批到达之前不完成工具，也不返回发送命令的结果。
    async def send_message(params: dict[str, Any]) -> commands.SessionSendMessageResult:
        command = commands.SessionSendMessageCommand.model_validate(params)
        if command.session_id not in sessions:
            raise HandlerError(INVALID_PARAMS, "Unknown fixture session")
        run_id = f"run-fixture-{uuid.uuid4().hex[:8]}"
        tool_id = f"tool-fixture-{uuid.uuid4().hex[:8]}"
        task = asyncio.current_task()
        if task is not None:
            running.add(task)
            session_tasks[command.session_id] = task
        metadata[command.session_id].running = True
        metadata[command.session_id].active_run_id = run_id
        try:
            content: Any = command.content
            if command.attachments:
                content = [{"type": "text", "text": content}, *[
                    {"type": "image", "source": {"type": "base64", "media_type": item.media_type, "data": item.data}}
                    for item in command.attachments
                ]]
            sessions[command.session_id].append({"role": "user", "content": content})
            await request_project()[1].handle(events.SessionMessageReceivedEvent(
                session_id=command.session_id, content=command.content, ts=now(),
            ))
            await request_project()[1].handle(events.RunStartedEvent(
                run_id=run_id, session_id=command.session_id, goal=command.content, ts=now(),
            ))
            await request_project()[1].handle(events.LlmModelSelectedEvent(
                run_id=run_id, model=metadata[command.session_id].model,
                strategy="static", ts=now(),
            ))
            introduction = "我先查看项目说明，确认前端需要对接的事件。\n\n"
            await stream(run_id, introduction)
            tool_params = {"path": "README.md"}
            await request_project()[1].handle(events.ToolCallStartedEvent(
                run_id=run_id, tool_use_id=tool_id, tool_name="read_file",
                params=tool_params, ts=now(),
            ))
            decision: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            permissions[tool_id] = (run_id, decision)
            metadata[command.session_id].pending_permissions = [commands.PendingPermission(
                tool_use_id=tool_id, tool_name="read_file", params=tool_params,
                param_preview="README.md（测试专用虚拟文件，不读取磁盘）", run_id=run_id,
            )]
            await request_project()[1].handle(events.PermissionRequestedEvent(
                run_id=run_id, tool_use_id=tool_id, tool_name="read_file", params=tool_params,
                param_preview="README.md（测试专用虚拟文件，不读取磁盘）",
                session_id=command.session_id, ts=now(),
            ))
            if await decision in {"allow_once", "always_allow"}:
                metadata[command.session_id].pending_permissions = []
                await request_project()[1].handle(events.ToolCallFinishedEvent(
                    run_id=run_id, tool_use_id=tool_id, tool_name="read_file", elapsed_ms=80,
                    output="# MiniClaude\n\n测试 README：使用事件流驱动的轻量 Agent。\n", ts=now(),
                ))
                conclusion = "README 已检查。前端通过 WebSocket 实时接收文本、工具状态和审批事件。你可以继续发送下一条消息。"
            else:
                await request_project()[1].handle(events.ToolCallFailedEvent(
                    run_id=run_id, tool_use_id=tool_id, tool_name="read_file", elapsed_ms=0,
                    error_class="permission_denied", error_message="用户拒绝了测试工具调用", ts=now(),
                ))
                conclusion = "已跳过被拒绝的文件读取。你可以继续发送下一条消息。"
            await stream(run_id, conclusion)
            sessions[command.session_id].append({
                "role": "assistant", "content": introduction + conclusion,
            })
            await request_project()[1].handle(events.RunFinishedEvent(
                run_id=run_id, status="success", steps=2, ts=now(),
            ))
            await request_project()[1].handle(events.SessionWaitingForInputEvent(
                session_id=command.session_id, last_run_id=run_id, ts=now(),
            ))
            return commands.SessionSendMessageResult(run_id=run_id)
        except asyncio.CancelledError:
            await request_project()[1].handle(events.RunFinishedEvent(
                run_id=run_id, status="failed", reason="cancelled", steps=1, ts=now(),
            ))
            return commands.SessionSendMessageResult(run_id=run_id, cancelled=True)
        finally:
            permissions.pop(tool_id, None)
            metadata[command.session_id].running = False
            metadata[command.session_id].pending_permissions = []
            metadata[command.session_id].active_run_id = None
            session_tasks.pop(command.session_id, None)
            if task is not None:
                running.discard(task)

    # 功能：关闭 fixture 时取消尚在等待审批的运行并停止真实 TCP 服务。
    # 设计：等待任务结束后释放连接，保证测试进程不残留内存任务或事件订阅。
    async def cleanup(app: web.Application) -> None:
        tasks = list(running)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for server in cores:
            await server.stop()
        temporary.cleanup()

    handlers = {
        "core.ping": ping,
        "event.subscribe": subscribe,
        "session.create": create_session,
        "session.get_history": history,
        "session.send_message": send_message,
        "permission.respond": respond,
        "session.list": list_sessions,
        "session.configure": configure,
        "session.rename": configure,
        "session.cancel": cancel,
        "session.delete": delete,
        "config.models": models,
        "plugins.list": plugins,
    }
    alternate = project_path / "another-project"
    default = project_path / "desktop-state/workspace"
    configs = {}
    for index, path in enumerate((project_path, alternate, default)):
        path.mkdir(parents=True, exist_ok=True)
        events_bus = broadcaster if index == 0 else IpcEventBroadcaster()
        server = core if index == 0 else SocketServer("127.0.0.1", 0, broadcaster=events_bus)
        if index:
            cores.append(server)
        for method, handler in handlers.items():
            server.register(method, handler)
        await server.start()
        assert isinstance(server._server, asyncio.Server)
        port = server._server.sockets[0].getsockname()[1]
        project_ports[port] = (path, events_bus)
        configs[path] = MiniConfig(port=port)
        configs[path].llm.default_model = "fixture-model"
    config = configs[project_path]
    workspace = Workspace(
        config, project_path, storage_path=project_path / "desktop-state",
        open_project=lambda path: configs[path], pick_project=lambda: str(project_path),
        default_path=default,
    )
    workspace._configs.update(configs)
    await workspace._select(alternate)
    await workspace._select(project_path)
    app = create_app(config, project_path=project_path, workspace=workspace)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_fixture_app(), host="127.0.0.1", port=7440)
