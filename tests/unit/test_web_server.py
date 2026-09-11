from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from mini_claude.core.config import MiniConfig
from mini_claude.web import server
from mini_claude.web.workspace import Workspace


# 功能：桌面重连时重新取得当前项目 Core，旧端口离线不会永久卡住会话入口。
# 设计：使用离线旧配置和真实新 TCP 后端，要求网关先调用项目启动器再转发会话命令。
async def test_desktop_reconnect_refreshes_core_before_forwarding(tmp_path: Path, free_port: int) -> None:
    received = []

    # 功能：新后端返回真实请求关联的会话响应。
    # 设计：等待网关转发内容，避免仅断言启动器调用而漏掉仍连接旧端口的错误。
    async def core_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                command = json.loads(line)
                received.append(command["method"])
                writer.write(json.dumps({"id": command["id"], "result": {"sessions": []}}).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(core_handler, "127.0.0.1", 0) as core:
        restored = MiniConfig(port=core.sockets[0].getsockname()[1])
        opener = MagicMock(return_value=restored)
        workspace = Workspace(MiniConfig(port=free_port), tmp_path, open_project=opener)
        app = server.create_app(workspace.config, tmp_path, workspace=workspace)
        async with TestClient(TestServer(app)) as client:
            origin = str(client.make_url("/")).rstrip("/")
            async with client.ws_connect("/ws", origin=origin) as ws:
                await ws.send_json({"id": 1, "method": "session.list", "params": {}})
                response = await asyncio.wait_for(ws.receive(), 3)
                assert response.type == WSMsgType.TEXT, "重连不应继续关闭到旧 Core 的连接"
                assert json.loads(response.data) == {"id": 1, "result": {"sessions": []}}
        opener.assert_called_once_with(tmp_path)
        assert workspace.config is restored
        assert received == ["session.list"]


# 功能：为网关测试提供与项目文件隔离的静态页面。
# 设计：临时目录只包含测试资源，防止测试依赖正在编辑的前端内容。
@pytest.fixture
def static_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    assets = tmp_path / "static"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>MiniClaude</title>")
    (assets / "app.js").write_text("console.log('ready')")
    (tmp_path / "private.txt").write_text("private")
    (assets / "escape.txt").symlink_to(tmp_path / "private.txt")
    monkeypatch.setattr(server, "STATIC_DIR", assets)
    return assets


# 功能：创建无 core 依赖的 HTTP 客户端并在用例结束后关闭服务。
# 设计：注入默认配置，测试过程中不加载 .env 或用户全局配置。
@pytest.fixture
async def client(static_dir: Path, tmp_path: Path, free_port: int) -> AsyncIterator[TestClient]:
    app = server.create_app(MiniConfig(port=free_port), project_path=tmp_path)
    async with TestClient(TestServer(app)) as connection:
        yield connection


# 功能：core 离线时首页和公开配置仍能加载。
# 设计：未启动 core，逐项检查公开字段和静态文件，排除配置对象的意外序列化。
async def test_http_remains_available_without_core(client: TestClient, tmp_path: Path) -> None:
    response = await client.get("/")
    assert response.status == 200
    assert "MiniClaude" in await response.text()
    response = await client.get("/assets/app.js")
    assert response.status == 200
    assert "ready" in await response.text()
    response = await client.get("/api/info")
    info = await response.json()
    assert set(info) == {"project_name", "project_path", "project_selected", "model", "core_host", "core_port"}
    assert info["project_path"] == str(tmp_path)
    assert info["project_name"] == tmp_path.name
    assert info["model"] == "claude-sonnet-4-6"


# 功能：移除最后一个项目先确认再广播空选择，重连后禁止把新会话发往残留 core。
# 设计：真实 WebSocket 与 TCP 假后端记录全部请求，验证界面禁用之外还有服务端执行边界。
async def test_last_project_removal_broadcasts_and_blocks_hidden_core(
    static_dir: Path, tmp_path: Path,
) -> None:
    received: list[str] = []

    # 功能：回复活动会话检查与订阅，保留连接直到网关主动关闭。
    # 设计：逐连接处理真实请求，确保移除检查的短连接与渲染器连接互不混淆。
    async def fake_core(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                command = json.loads(line)
                received.append(command["method"])
                result = {"sessions": []} if command["method"] == "session.list" else {}
                writer.write(json.dumps({"id": command["id"], "result": result}).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(fake_core, "127.0.0.1", 0) as core:
        config = MiniConfig(port=core.sockets[0].getsockname()[1])
        app = server.create_app(config, project_path=tmp_path)
        async with TestClient(TestServer(app)) as connection:
            origin = str(connection.make_url("/")).rstrip("/")
            async with connection.ws_connect("/ws", origin=origin) as ws:
                await ws.send_json({"id": 1, "method": "workspace.remove", "params": {"path": str(tmp_path)}})
                response = await asyncio.wait_for(ws.receive_json(), 2)
                assert response["id"] == 1 and response["result"]["project_path"] is None
                assert (await ws.receive_json())["event"]["type"] == "workspace.projects_changed"
                changed = (await ws.receive_json())["event"]
                assert changed["type"] == "workspace.changed" and changed["project_selected"] is False
                assert (await ws.receive()).type == WSMsgType.CLOSE
            info = await (await connection.get("/api/info")).json()
            assert info["project_path"] is None
            async with connection.ws_connect("/ws", origin=origin) as ws:
                await ws.send_json({"id": 2, "method": "session.create", "params": {}})
                assert "先选择" in (await ws.receive_json())["error"]["message"]
                await ws.send_json({"id": 3, "method": "event.subscribe", "params": {}})
                assert (await ws.receive_json())["id"] == 3
    assert received == ["session.list", "event.subscribe"]


# 功能：后端尚未登记的消息提交也会阻止移除项目，避免跨连接活动检查漏掉正在提交的任务。
# 设计：主连接保留发送请求不响应，检查连接返回空会话列表以精确覆盖时间窗口。
async def test_pending_message_submission_blocks_project_removal(
    static_dir: Path, tmp_path: Path,
) -> None:
    submitted = asyncio.Event()

    # 功能：模拟消息已经接收但会话状态尚未公布的 core。
    # 设计：使用事件保证移除发生在提交之后，使测试不依赖任务调度速度。
    async def fake_core(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                command = json.loads(line)
                if command["method"] == "session.send_message":
                    submitted.set()
                else:
                    writer.write(json.dumps({"id": command["id"], "result": {"sessions": []}}).encode() + b"\n")
                    await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(fake_core, "127.0.0.1", 0) as core:
        app = server.create_app(MiniConfig(port=core.sockets[0].getsockname()[1]), tmp_path)
        async with TestClient(TestServer(app)) as connection:
            origin = str(connection.make_url("/")).rstrip("/")
            async with connection.ws_connect("/ws", origin=origin) as ws:
                await ws.send_json({"id": 1, "method": "session.send_message"})
                await asyncio.wait_for(submitted.wait(), 2)
                await ws.send_json({"id": 2, "method": "workspace.remove", "params": {"path": str(tmp_path)}})
                response = await asyncio.wait_for(ws.receive_json(), 2)
                assert response["id"] == 2 and "提交的任务" in response["error"]["message"]
                assert app[server.WORKSPACE].project_selected


# 功能：移除当前项目后的旧连接不能向旧 core 发消息，也不能在新项目中创建计划或读取文件。
# 设计：直接提交移除以停留在广播关闭前的窗口，随后用真实旧 WebSocket 覆盖三类项目命令。
async def test_stale_socket_cannot_act_during_project_removal_transition(
    static_dir: Path, tmp_path: Path,
) -> None:
    received: list[str] = []
    second = tmp_path / "second"
    second.mkdir()

    # 功能：记录移除检查与所有意外透传的项目命令。
    # 设计：后端只返回空会话列表，测试结束时可直接断言没有任何旧项目操作进入 core。
    async def fake_core(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                command = json.loads(line)
                received.append(command["method"])
                writer.write(json.dumps({"id": command["id"], "result": {"sessions": []}}).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(fake_core, "127.0.0.1", 0) as core:
        config = MiniConfig(port=core.sockets[0].getsockname()[1])
        workspace = Workspace(config, tmp_path, open_project=lambda _: config)
        await workspace._select(second)
        await workspace._select(tmp_path)
        app = server.create_app(config, tmp_path, workspace=workspace)
        async with TestClient(TestServer(app)) as connection:
            origin = str(connection.make_url("/")).rstrip("/")
            async with connection.ws_connect("/ws", origin=origin) as ws:
                await workspace.handle("workspace.remove", {"path": str(tmp_path)})
                assert workspace.project_path == second
                for index, method in enumerate(["session.create", "schedules.create", "workspace.files"]):
                    await ws.send_json({"id": index, "method": method, "params": {}})
                    response = await asyncio.wait_for(ws.receive_json(), 2)
                    assert "已切换" in response["error"]["message"]
    assert received == ["session.list"]


# 功能：首页禁止被嵌入 iframe，防止外站点击劫持本地审批。
# 设计：检查实际 HTTP 响应同时携带现代浏览器 CSP 和兼容旧浏览器的拒绝嵌入头。
async def test_homepage_cannot_be_framed(client: TestClient) -> None:
    response = await client.get("/")
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert response.headers["X-Frame-Options"] == "DENY"


# 功能：拒绝 DNS 重绑定与外站 Origin 对本地网关的访问。
# 设计：直接覆盖 HTTP 请求头，覆盖恶意域名、空 Origin 与本地但不同端口的来源。
@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example"},
    {"Host": "127.0.0.1.attacker.example"},
    {"Origin": "https://attacker.example"},
    {"Origin": "null"},
    {"Origin": "http://127.0.0.1:1"},
])
async def test_rejects_untrusted_request_headers(client: TestClient, headers: dict[str, str]) -> None:
    response = await client.get("/api/info", headers=headers)
    assert response.status == 403


# 功能：静态路由不能通过 URL 编码或软链接读取静态目录外文件。
# 设计：分别请求带编码分隔符的路径、逃逸软链接及不存在的资源。
@pytest.mark.parametrize("path", [
    "/assets/..%2Fprivate.txt", "/assets/escape.txt", "/assets/missing.js", "/assets/.env",
])
async def test_static_files_cannot_escape_directory(client: TestClient, path: str) -> None:
    response = await client.get(path)
    assert response.status == 404
    assert "private" not in await response.text()


# 功能：WebSocket 强制浏览器提供同源 Origin，离线时给出可识别的关闭原因。
# 设计：先验证缺少 Origin 的升级被拒，再用合法来源连接尚未启动的 core。
async def test_websocket_requires_origin_and_reports_offline_core(client: TestClient) -> None:
    response = await client.get("/ws")
    assert response.status == 403
    async with client.ws_connect("/ws", origin=str(client.make_url("/")).rstrip("/")) as ws:
        message = await asyncio.wait_for(ws.receive(), timeout=2)
        assert message.type == WSMsgType.CLOSE
        assert message.data == 1013
        assert "mini-core" in message.extra


# 功能：事件与命令并行透传，长运行消息尚未响应时审批指令仍能送达。
# 设计：假 core 收到发送指令后仅推送审批事件，等待后续审批命令再一次写回两个响应。
async def test_bridges_events_and_concurrent_commands(static_dir: Path, tmp_path: Path) -> None:
    received: list[dict[str, object]] = []
    disconnected = asyncio.Event()
    event = '{"type":"event","event":"permission.requested","data":{"id":"p1"}}'

    # 功能：模拟等待审批的 core 会话。
    # 设计：保持第一条请求悬而未决，直到第二条请求到达才发送响应。
    async def fake_core(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            received.append(json.loads(await reader.readline()))
            writer.write((event + "\n").encode())
            await writer.drain()
            received.append(json.loads(await reader.readline()))
            writer.write(b'{"jsonrpc":"2.0","id":2,"result":{}}\n')
            writer.write(b'{"jsonrpc":"2.0","id":1,"result":{"done":true}}\n')
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            disconnected.set()

    async with await asyncio.start_server(fake_core, "127.0.0.1", 0) as core:
        port = core.sockets[0].getsockname()[1]
        app = server.create_app(MiniConfig(port=port), project_path=tmp_path)
        async with TestClient(TestServer(app)) as connection:
            origin = str(connection.make_url("/")).rstrip("/")
            async with connection.ws_connect("/ws", origin=origin) as ws:
                await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "session.send_message"})
                assert (await asyncio.wait_for(ws.receive(), 2)).data == event
                await ws.send_json({"jsonrpc": "2.0", "id": 2, "method": "permission.respond"})
                assert (await asyncio.wait_for(ws.receive_json(), 2))["id"] == 2
                assert (await asyncio.wait_for(ws.receive_json(), 2))["id"] == 1
            await asyncio.wait_for(disconnected.wait(), 2)
    assert [request["method"] for request in received] == ["session.send_message", "permission.respond"]


# 功能：无效 JSON、批量对象和多行帧不会注入 core 的 NDJSON 命令流。
# 设计：假 core 仅记录字节，非法帧必须在网关关闭且未向 core 发送任何数据。
@pytest.mark.parametrize("payload", ["invalid", "[]", '{}\n{}', '{\n"id":1}'])
async def test_invalid_messages_do_not_reach_core(
    static_dir: Path, tmp_path: Path, payload: str,
) -> None:
    received: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    # 功能：捕获网关关闭前发送的全部字节。
    # 设计：读取到 EOF 后完成 Future，确保断言发生在连接清理之后。
    async def fake_core(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.set_result(await reader.read())
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(fake_core, "127.0.0.1", 0) as core:
        app = server.create_app(MiniConfig(port=core.sockets[0].getsockname()[1]), tmp_path)
        async with TestClient(TestServer(app)) as connection:
            async with connection.ws_connect("/ws", origin=str(connection.make_url("/"))[:-1]) as ws:
                await ws.send_str(payload)
                message = await asyncio.wait_for(ws.receive(), 2)
                assert message.type == WSMsgType.CLOSE
                assert message.data == 1007
            assert await asyncio.wait_for(received, 2) == b""


# 功能：限制浏览器消息大小并拒绝二进制协议帧，避免耗尽网关内存。
# 设计：用较小的测试上限发送超限文本或二进制数据，验证标准 WebSocket 关闭码。
@pytest.mark.parametrize(("binary", "expected_code"), [(False, 1009), (True, 1003)])
async def test_rejects_oversized_and_binary_frames(
    static_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    binary: bool, expected_code: int,
) -> None:
    monkeypatch.setattr(server, "MAX_COMMAND_BYTES", 32)
    disconnected = asyncio.Event()

    # 功能：保持 core 连接可用直到网关清理。
    # 设计：不主动发出事件，确保关闭来自浏览器帧校验。
    async def fake_core(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        writer.close()
        await writer.wait_closed()
        disconnected.set()

    async with await asyncio.start_server(fake_core, "127.0.0.1", 0) as core:
        app = server.create_app(MiniConfig(port=core.sockets[0].getsockname()[1]), tmp_path)
        async with TestClient(TestServer(app)) as connection:
            async with connection.ws_connect("/ws", origin=str(connection.make_url("/"))[:-1]) as ws:
                if binary:
                    await ws.send_bytes(b"{}")
                else:
                    await ws.send_str('{"text":"' + "x" * 32 + '"}')
                message = await asyncio.wait_for(ws.receive(), 2)
                assert message.type == WSMsgType.CLOSE
                assert message.data == expected_code
            await asyncio.wait_for(disconnected.wait(), 2)


# 功能：core 中途断开时主动通知浏览器，不留下无响应的连接。
# 设计：假 core 接受连接后立即断开，浏览器应收到服务端错误关闭码和原因。
async def test_core_disconnect_closes_browser(static_dir: Path, tmp_path: Path) -> None:
    # 功能：模拟 core 进程退出导致的 TCP EOF。
    # 设计：连接建立后直接关闭写流，不依赖固定时间等待。
    async def fake_core(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(fake_core, "127.0.0.1", 0) as core:
        app = server.create_app(MiniConfig(port=core.sockets[0].getsockname()[1]), tmp_path)
        async with TestClient(TestServer(app)) as connection:
            async with connection.ws_connect("/ws", origin=str(connection.make_url("/"))[:-1]) as ws:
                message = await asyncio.wait_for(ws.receive(), 2)
                assert message.type == WSMsgType.CLOSE
                assert message.data == 1011
                assert "mini-core disconnected" in message.extra


# 功能：关闭 Web 服务时同步释放浏览器与 core 连接，避免残留订阅。
# 设计：浏览器保持连接时触发 app.shutdown，并等待 core 读到 EOF 后再完成断言。
async def test_shutdown_releases_active_connections(static_dir: Path, tmp_path: Path) -> None:
    connected = asyncio.Event()
    disconnected = asyncio.Event()

    # 功能：跟踪 core 连接从建立到释放的完整生命周期。
    # 设计：用 Event 同步，避免在网关尚未建立 TCP 连接时触发关闭。
    async def fake_core(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connected.set()
        await reader.read()
        writer.close()
        await writer.wait_closed()
        disconnected.set()

    async with await asyncio.start_server(fake_core, "127.0.0.1", 0) as core:
        app = server.create_app(MiniConfig(port=core.sockets[0].getsockname()[1]), tmp_path)
        async with TestClient(TestServer(app)) as connection:
            async with connection.ws_connect("/ws", origin=str(connection.make_url("/"))[:-1]) as ws:
                await asyncio.wait_for(connected.wait(), 2)
                shutdown = asyncio.create_task(app.shutdown())
                message = await asyncio.wait_for(ws.receive(), 2)
                assert message.type == WSMsgType.CLOSE
                assert message.data == 1001
                await asyncio.wait_for(shutdown, 2)
                await asyncio.wait_for(disconnected.wait(), 2)


# 功能：原生项目切换先返回结果再通知重连，新连接必须连接新项目 core。
# 设计：启动两个真实 TCP 假 core，检查桌面 RPC 不泄漏到 core 且新请求确实路由至第二个端口。
async def test_workspace_switch_routes_reconnected_websocket(
    static_dir: Path, tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    requests: list[dict[str, object]] = []

    # 功能：回显收到请求的本地端口，供测试辨别真正连接的项目后端。
    # 设计：允许零条请求直到连接关闭，验证工作区控制命令由网关处理。
    async def fake_core(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                request = json.loads(line)
                requests.append(request)
                writer.write(json.dumps({"id": request["id"], "result": {
                    "port": writer.get_extra_info("sockname")[1],
                }}).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(fake_core, "127.0.0.1", 0) as first_core:
        async with await asyncio.start_server(fake_core, "127.0.0.1", 0) as second_core:
            first_config = MiniConfig(port=first_core.sockets[0].getsockname()[1])
            second_config = MiniConfig(port=second_core.sockets[0].getsockname()[1])
            workspace = Workspace(first_config, first, open_project=lambda _: second_config,
                                  pick_project=lambda: str(second))
            app = server.create_app(first_config, first, workspace=workspace)
            async with TestClient(TestServer(app)) as client:
                origin = str(client.make_url("/"))[:-1]
                async with client.ws_connect("/ws", origin=origin) as ws:
                    await ws.send_json({"jsonrpc": "2.0", "id": "switch", "method": "workspace.pick"})
                    response = await asyncio.wait_for(ws.receive_json(), 2)
                    assert response["id"] == "switch"
                    assert response["result"]["project_path"] == str(second)
                    changed = await asyncio.wait_for(ws.receive_json(), 2)
                    assert changed["event"]["type"] == "workspace.changed"
                    assert (await asyncio.wait_for(ws.receive(), 2)).data == 1012
                info = await (await client.get("/api/info")).json()
                assert info["project_path"] == str(second)
                async with client.ws_connect("/ws", origin=origin) as ws:
                    await ws.send_json({"jsonrpc": "2.0", "id": "ping", "method": "core.ping"})
                    response = await asyncio.wait_for(ws.receive_json(), 2)
                    assert response["result"]["port"] == second_config.port
    assert [item["method"] for item in requests] == ["core.ping"]


# 功能：工作区参数错误以 JSON-RPC 错误返回，随后核心事件连接仍可继续使用。
# 设计：同一个 WebSocket 先发送无效目录切换再查询项目，确保不会因用户输入关闭通道。
async def test_invalid_workspace_command_keeps_connection(
    static_dir: Path, tmp_path: Path,
) -> None:
    # 功能：保持无业务请求的 core 通道直到浏览器退出。
    # 设计：接收 EOF 后清理写流，不制造真实项目任务。
    async def fake_core(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(fake_core, "127.0.0.1", 0) as core:
        app = server.create_app(MiniConfig(port=core.sockets[0].getsockname()[1]), tmp_path)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/ws", origin=str(client.make_url("/"))[:-1]) as ws:
                await ws.send_json({"id": 1, "method": "workspace.select", "params": {"path": "/invalid"}})
                assert "error" in await asyncio.wait_for(ws.receive_json(), 2)
                await ws.send_json({"id": 2, "method": "workspace.list"})
                result = (await asyncio.wait_for(ws.receive_json(), 2))["result"]
                assert result["current_path"] == str(tmp_path)
                await ws.send_json({"id": 3, "method": "schedules.list", "params": {"type": "schedules.list"}})
                result = (await asyncio.wait_for(ws.receive_json(), 2))["result"]
                assert result["schedules"] == []
                await ws.send_json({"id": 4, "method": "schedules.create", "params": {
                    "type": "schedules.create", "title": "Review", "prompt": "Review project",
                    "next_run": "2099-01-01T08:00:00+08:00", "repeat": "once",
                }})
                event = await asyncio.wait_for(ws.receive_json(), 2)
                assert event["event"]["type"] == "schedules.changed"
                result = (await asyncio.wait_for(ws.receive_json(), 2))["result"]
                assert result["schedule"]["title"] == "Review"
