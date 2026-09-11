from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession, ClientWebSocketResponse

from mini_claude.core.config import MiniConfig
from mini_claude.desktop.app import CoreProcess, DesktopGateway, ProjectCores
from mini_claude.desktop.core import DEFAULT_CONNECTION_ENV
from mini_claude.web.workspace import Workspace


# 功能：运行真实 Core 子进程，并在测试后回收它。
# 设计：仅把全局磁盘路径重定向到临时目录，环境白名单排除用户密钥、代理和配置。
@pytest.fixture
def isolated_core(tmp_path: Path, free_port: int) -> Iterator[CoreProcess]:
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    state = tmp_path / "state"
    (bootstrap / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        "_original = Path.expanduser\n"
        "def _isolated(self):\n"
        "    value = str(self)\n"
        "    if value == '~/.mini' or value.startswith('~/.mini/'):\n"
        f"        return Path({str(state)!r}) / value.removeprefix('~/.mini').lstrip('/')\n"
        "    return _original(self)\n"
        "Path.expanduser = _isolated\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    source = Path(__file__).resolve().parents[2] / "src"
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": os.pathsep.join((str(bootstrap), str(source))),
        "MINI_CONFIG": str(tmp_path / "missing.toml"),
        "MINI_TRACE_ENABLED": "false",
        "MINI_LOG_FILE": str(tmp_path / "core.log"),
        "MINI_LOG_LEVEL": "CRITICAL",
        DEFAULT_CONNECTION_ENV: "{}",
    }
    core = CoreProcess(MiniConfig(port=free_port), project, environment=environment)
    try:
        core.start()
        yield core
    finally:
        core.stop()


# 功能：向真实 Core 发送一次 RPC 并返回业务结果。
# 设计：使用有界短连接，明确拒绝无响应和错误响应。
def core_request(core: CoreProcess, method: str, **params: Any) -> dict[str, Any]:
    with socket.create_connection((core.config.host, core.config.port), timeout=3) as connection:
        connection.settimeout(5)
        with connection.makefile("rwb") as stream:
            stream.write(json.dumps({
                "jsonrpc": "2.0", "id": "runtime-test", "method": method,
                "params": {"type": method, **params},
            }).encode() + b"\n")
            stream.flush()
            line = stream.readline()
    assert line, "Core exited or disconnected without a response"
    response = json.loads(line)
    assert "error" not in response, response
    result: dict[str, Any] = response["result"]
    return result


# 功能：验证缺少连接时运行正常结束且后端还能处理后续操作。
# 设计：真实生产发送链路不调用外部服务，并检查会话运行状态被复位。
def test_missing_connection_finishes_run_without_killing_real_core(
    isolated_core: CoreProcess,
) -> None:
    core = isolated_core
    sid = core_request(core, "session.create", mode="chat")["session_id"]
    result = core_request(core, "session.send_message", session_id=sid, content="test")
    assert result["status"] == "failed"
    assert result["reason"] == "llm_error"
    assert core.available()
    sessions = core_request(core, "session.list")["sessions"]
    assert sessions[0]["status"] == "waiting_for_input"
    assert sessions[0]["running"] is False
    assert core_request(core, "session.create", mode="chat")["session_id"] != sid


# 功能：验证重新打开已退出的后端会恢复它和原有会话。
# 设计：实际结束桌面拥有的子进程，随后验证新进程和再次打开时的复用。
def test_reopening_exited_owned_core_restarts_and_restores_sessions(
    isolated_core: CoreProcess,
) -> None:
    core = isolated_core
    registry = ProjectCores(core.project_path, core, core.environment)
    try:
        sid = core_request(core, "session.create", title="before restart")["session_id"]
        process = core.process
        assert process is not None
        process.terminate()
        process.wait(timeout=5)
        restored = registry.open(core.project_path)
        assert restored is core.config
        assert core.available(), "Cached Core must be restarted after its process exits"
        assert core.process is not process
        assert core_request(core, "session.list")["sessions"][0]["session_id"] == sid
        registry.open(core.project_path)
        assert core.process is not None
        resumed = core.process
        registry.open(core.project_path)
        assert core.process is resumed
    finally:
        registry.stop()


# 功能：通过生产桌面网关发送 RPC 并返回结果。
# 设计：请求不订阅事件，确保收到的第一帧是当前命令响应。
async def gateway_request(
    connection: ClientWebSocketResponse, method: str, **params: Any,
) -> dict[str, Any]:
    await connection.send_json({
        "jsonrpc": "2.0", "id": "gateway-runtime", "method": method,
        "params": {"type": method, **params},
    })
    response = await connection.receive_json(timeout=5)
    assert response.get("id") == "gateway-runtime"
    assert "error" not in response, response
    result: dict[str, Any] = response["result"]
    return result


# 功能：验证真实桌面 WebSocket 断线重连可自动恢复 Core 与会话历史。
# 设计：仅使用隔离 Core 和随机网关端口，经历创建、发送、进程退出、重连与历史查询。
async def test_gateway_reconnect_restarts_real_core_and_restores_history(
    isolated_core: CoreProcess, tmp_path: Path,
) -> None:
    core = isolated_core
    registry = ProjectCores(core.project_path, core, core.environment)
    workspace = Workspace(
        core.config, core.project_path, storage_path=tmp_path / "desktop",
        open_project=registry.open,
    )
    gateway = DesktopGateway(core.config, core.project_path, port=0, workspace=workspace)
    try:
        url = gateway.start()
        async with ClientSession() as client:
            async with client.ws_connect(f"{url}/ws", headers={"Origin": url}) as connection:
                created = await gateway_request(connection, "session.create", mode="chat")
                sid = created["session_id"]
                sent = await gateway_request(
                    connection, "session.send_message", session_id=sid,
                    content="preserve this history after restart",
                )
                assert sent["status"] == "failed"
                process = core.process
                assert process is not None
                process.terminate()
                await asyncio.to_thread(process.wait, timeout=5)
                await connection.receive(timeout=5)
            async with client.ws_connect(f"{url}/ws", headers={"Origin": url}) as connection:
                listed = await gateway_request(connection, "session.list")
                assert listed["sessions"][0]["session_id"] == sid
                history = await gateway_request(connection, "session.get_history", session_id=sid)
                assert history["messages"] == [{
                    "role": "user", "content": "preserve this history after restart",
                }]
                assert core.process is not process
    finally:
        gateway.stop()
        registry.stop()
