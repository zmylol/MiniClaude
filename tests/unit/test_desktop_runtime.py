from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mini_claude.core.config import MiniConfig
from mini_claude.desktop.app import CoreProcess, ProjectCores
from mini_claude.desktop.core import DEFAULT_CONNECTION_ENV


@pytest.fixture
def isolated_core(tmp_path: Path, free_port: int) -> Iterator[CoreProcess]:
    # 使用真实生产入口和进程，仅把全局磁盘路径重定向到临时目录。
    # 子进程环境白名单确保不会继承用户密钥、代理或项目配置。
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
