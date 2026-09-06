from __future__ import annotations

import asyncio
import base64
import json
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from mini_claude.core.app import CoreApp
from mini_claude.core.bus.commands import SessionSendMessageCommand
from mini_claude.core.bus.envelope import HandlerError
from mini_claude.core.config import MiniConfig
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.types import LlmResponse, ToolCallBlock
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.runner import AgentRunner, RunOutcome
from mini_claude.core.session.manager import SessionManager
from mini_claude.core.session.store import SessionStore
from mini_claude.core.tools.builtin.bash import BashTool


class ReplyRunner:
    # 返回固定回复并写入真实会话存储，避免测试使用外部模型
    async def run_and_capture(self, goal: str, **kwargs: Any) -> RunOutcome:
        kwargs["store"].append_message(kwargs["session"].id, "assistant", "reply")
        return RunOutcome(status="success", result="reply", reason=None)


# 构建使用真实持久化存储且不访问外部 API 的会话管理器


def manager_for(root: Path, project: Path) -> SessionManager:
    return SessionManager(
        SessionStore(root), ReplyRunner, EventBus(), project_path=project, default_model="model-one"
    )  # type: ignore[arg-type]


# 功能：验证跨重启会话继续发送，且不会加载其他项目或无归属的旧会话
# 设计：两个项目共用真实存储并重建管理器，覆盖身份隔离与恢复后的完整发送路径
async def test_restart_resume_and_project_isolation(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    first = manager_for(root, tmp_path / "one")
    session = await first.create("chat")
    await first.send_message(session.id, "hello")
    await first.rename(session.id, "renamed")
    await first.configure(session.id, model="model-two", permission_mode="read_only")
    second = manager_for(root, tmp_path / "two")
    other = await second.create("chat")
    legacy = await first.create("chat")
    meta = root / legacy.id / "meta.json"
    data = json.loads(meta.read_text())
    data.pop("project_path")
    meta.write_text(json.dumps(data))

    restored = manager_for(root, tmp_path / "one")
    assert [item.id for item in restored.list_sessions()] == [session.id]
    loaded = restored.list_sessions()[0]
    assert (loaded.title, loaded.model, loaded.permission_mode) == (
        "renamed",
        "model-two",
        "read_only",
    )
    assert await restored.get_history(session.id) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "reply"},
    ]
    await restored.send_message(session.id, "continue")
    assert len(await restored.get_history(session.id)) == 4
    with pytest.raises(HandlerError):
        await restored.get_history(other.id)
    await restored.delete(session.id)
    assert not (root / session.id).exists()
    assert (root / other.id).exists()


# 功能：验证畸形会话 ID 和符号链接无法越过会话存储目录边界
# 设计：覆盖直接存储调用与 RPC 管理入口，确保删除与读取不访问外部路径
async def test_session_paths_reject_traversal_and_symlinks(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "sessions" / "sess-link").symlink_to(outside, target_is_directory=True)
    for sid in ("../outside", "/tmp", "sess-../outside", "sess-link"):
        with pytest.raises(ValueError):
            store.session_dir(sid)
    manager = manager_for(tmp_path / "sessions", tmp_path)
    assert manager.list_sessions() == []
    with pytest.raises(HandlerError):
        await manager.delete("../outside")
    assert outside.exists()


# 功能：验证停止正在等待审批的真实运行会清理 Future、发送终态且允许继续会话
# 设计：真实 Runner 与权限管理器配合工具调用型假 provider，避免只测试取消一个空协程
async def test_cancel_pending_approval_stops_run_and_can_resume(tmp_path: Path) -> None:
    bus = EventBus()
    requested = asyncio.Event()
    events: list[BaseModel] = []

    # 收集所有运行事件并在审批到达时通知测试发出停止
    async def collect(event: BaseModel) -> None:
        events.append(event)
        if event.type == "permission.requested":
            requested.set()

    bus.subscribe(collect)
    permissions = PermissionManager(timeout_s=0)

    class Provider:
        finished = False
        resumed_messages: list[dict[str, Any]] = []

        # 首轮请求执行 bash，恢复后返回最终回复
        async def chat(self, **kwargs: Any) -> LlmResponse:
            if self.finished:
                self.resumed_messages = kwargs["messages"]
                return LlmResponse(stop_reason="end_turn", text="resumed")
            return LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    ToolCallBlock(id="approval", name="bash", input={"command": "echo never"})
                ],
            )

    provider = Provider()
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store,
        lambda: AgentRunner(
            MiniConfig(), provider=provider, bus=bus, permission_manager=permissions
        ),
        bus,
        project_path=tmp_path,
        permission_manager=permissions,
    )
    session = await manager.create("chat")
    sending = asyncio.create_task(manager.send_message(session.id, "run"))
    await asyncio.wait_for(requested.wait(), 3)
    assert manager.has_active_runs()
    with pytest.raises(HandlerError):
        await manager.configure(session.id, permission_mode="full_access")
    assert await manager.cancel(session.id)
    run_id = await asyncio.wait_for(sending, 3)
    assert manager.was_cancelled(session.id)
    assert permissions._pending == {}
    finished = [event for event in events if event.type == "run.finished"]
    assert len(finished) == 1 and finished[0].reason == "cancelled"
    assert finished[0].run_id == run_id
    assert not manager.has_active_runs()
    provider.finished = True
    await manager.send_message(session.id, "continue")
    assert not manager.was_cancelled(session.id)
    assert any(message["content"] == "continue" for message in provider.resumed_messages)
    cancelled_results = [
        block
        for message in provider.resumed_messages
        if isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    assert cancelled_results[0]["tool_use_id"] == "approval"
    assert cancelled_results[0]["is_error"] is True


# 功能：验证停止会话也能结束主回复完成后仍在执行的后台子代理
# 设计：真实 spawn_agent 工具启动挂起子代理，直接检查其完成事件与活动注册状态
async def test_cancel_includes_background_subagents(tmp_path: Path) -> None:
    bus = EventBus()
    child_started = asyncio.Event()
    events: list[BaseModel] = []

    # 保存子代理终态以验证取消时仍发布完成事件
    async def collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(collect)

    class Provider:
        root_id: str | None = None

        # 主代理派生后台任务后结束，子代理一直等待取消
        async def chat(self, run_id: str, step: int, **kwargs: Any) -> LlmResponse:
            self.root_id = self.root_id or run_id
            if run_id != self.root_id:
                child_started.set()
                await asyncio.Event().wait()
            if step == 1:
                return LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCallBlock(
                            id="spawn",
                            name="spawn_agent",
                            input={
                                "description": "child",
                                "prompt": "wait",
                                "run_in_background": True,
                            },
                        )
                    ],
                )
            return LlmResponse(stop_reason="end_turn", text="launched")

    provider = Provider()
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"),
        lambda: AgentRunner(MiniConfig(), provider=provider, bus=bus),
        bus,
        project_path=tmp_path,
    )
    session = await manager.create("chat")
    await manager.send_message(session.id, "launch")
    await asyncio.wait_for(child_started.wait(), 3)
    assert manager.has_active_runs()
    assert await manager.cancel(session.id)
    assert not manager.has_active_runs()
    assert any(event.type == "subagent.finished" and event.status == "failed" for event in events)


# 功能：验证只读与完全访问模式真正改变工具授权，且无法被历史授权缓存绕过
# 设计：植入持久化放行记录后测试只读拒绝，再验证完全访问不会产生审批事件
async def test_permission_modes_enforce_real_tool_access() -> None:
    manager = PermissionManager(timeout_s=0)
    manager._persistent_always["bash"] = "allow"

    # 自动模式不得进入审批事件路径
    async def unexpected_event(event: dict[str, Any]) -> None:
        pytest.fail("automatic mode unexpectedly requested permission")

    manager.set_session_mode("session", "read_only")
    for tool, expected in (
        ("bash", False),
        ("write_file", False),
        ("mcp_server_tool", False),
        ("read_file", True),
        ("list_dir", True),
    ):
        allowed, _ = await manager.check_and_wait(tool, tool, {}, "session", unexpected_event)
        assert allowed is expected
    manager.set_session_mode("session", "full_access")
    allowed, _ = await manager.check_and_wait(
        "bash", "bash", {"command": "echo hello"}, "session", unexpected_event
    )
    assert allowed


# 功能：验证所选会话模型传给真实 provider 工厂且不改变全局默认配置
# 设计：仅替换外部客户端构造器，保留真实 Runner 的配置和调用路径
async def test_session_model_overrides_provider_without_mutating_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected: list[str] = []

    class Provider:
        # 记录 Runner 实际使用的模型标识
        def __init__(self, model: str) -> None:
            selected.append(model)

        # 返回固定回复，确保测试不会调用网络
        async def chat(self, **kwargs: Any) -> LlmResponse:
            return LlmResponse(stop_reason="end_turn", text="done")

    monkeypatch.setattr("mini_claude.core.runner.AnthropicProvider", Provider)
    config = MiniConfig()
    original_model = config.llm.default_model
    bus = EventBus()
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"),
        lambda: AgentRunner(config, bus=bus),
        bus,
        project_path=tmp_path,
    )
    first = await manager.create("chat", model="custom-model")
    await manager.send_message(first.id, "hello")
    assert selected == ["custom-model"]
    assert config.llm.default_model == original_model


# 功能：验证图片附件以真实多模态消息保存且畸形内容在协议边界被拒绝
# 设计：通过 CoreApp handler 走解析到存储的完整路径，再分别破坏编码与 MIME
async def test_images_are_validated_and_stored_as_content_blocks(tmp_path: Path) -> None:
    app = CoreApp()
    app._sessions = manager_for(tmp_path / "sessions", tmp_path)
    created = await app._session_create_handler({})
    attachment = {
        "name": "image.png",
        "media_type": "image/png",
        "data": base64.b64encode(b"\x89PNG\r\n\x1a\ntest").decode(),
    }
    params = {"session_id": created.session_id, "content": "explain", "attachments": [attachment]}
    await app._session_send_handler(params)
    history = await app._session_history_handler({"session_id": created.session_id})
    assert history.messages[0]["content"][1]["source"]["data"] == attachment["data"]
    image_only = await app._session_create_handler({})
    await app._session_send_handler({**params, "session_id": image_only.session_id, "content": ""})
    image_history = await app._session_history_handler({"session_id": image_only.session_id})
    assert image_history.messages[0]["content"][0]["type"] == "image"
    with pytest.raises(ValidationError):
        SessionSendMessageCommand(session_id=created.session_id, content=" ")
    for invalid in (
        {**attachment, "data": "not base64!"},
        {**attachment, "media_type": "image/jpeg"},
    ):
        with pytest.raises(ValidationError):
            SessionSendMessageCommand.model_validate({**params, "attachments": [invalid]})


# 功能：验证停止 bash 会杀死整个进程组，后代进程不再继续写文件
# 设计：实际子进程周期写心跳，停止后检查心跳文件保持不变，覆盖 shell 派生进程的清理
async def test_cancel_bash_kills_shell_descendants(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat"
    script = (
        "import pathlib,time\np=pathlib.Path("
        + repr(str(heartbeat))
        + ")\nwhile True:\n p.open('a').write('x')\n time.sleep(.02)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    task = asyncio.create_task(BashTool().invoke({"command": command}))
    for _ in range(100):
        if heartbeat.exists():
            break
        await asyncio.sleep(0.01)
    assert heartbeat.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    snapshot = heartbeat.read_bytes()
    await asyncio.sleep(0.1)
    assert heartbeat.read_bytes() == snapshot


# 功能：验证桌面会话命令通过真实 IPC 连接完成创建、改名、配置、列举和删除
# 设计：使用实际 SocketServer 注册 CoreApp handler，确认 wire 字段与前端合同一致
async def test_session_lifecycle_over_real_ipc(tmp_path: Path) -> None:
    from mini_claude.core.transport.socket_server import SocketServer

    app = CoreApp()
    app._config = MiniConfig()
    app._sessions = manager_for(tmp_path / "sessions", tmp_path)
    server = SocketServer("127.0.0.1", 0)
    for method, handler in (
        ("session.create", app._session_create_handler),
        ("session.list", app._session_list_handler),
        ("session.rename", app._session_rename_handler),
        ("session.configure", app._session_configure_handler),
        ("session.delete", app._session_delete_handler),
    ):
        server.register(method, handler)
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    # 发送一个 JSON-RPC 请求并返回同一连接收到的成功结果
    async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        writer.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": method, "method": method, "params": params}
            ).encode()
            + b"\n"
        )
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 3))
        assert response["id"] == method and "error" not in response
        return response["result"]

    try:
        created = await request(
            "session.create", {"model": "chosen-model", "permission_mode": "read_only"}
        )
        sid = created["session_id"]
        assert created["model"] == "chosen-model"
        await request("session.rename", {"session_id": sid, "title": "My chat"})
        configured = await request(
            "session.configure",
            {"session_id": sid, "model": "next-model", "permission_mode": "ask"},
        )
        assert configured["session"]["model"] == "next-model"
        listed = await request("session.list", {})
        assert listed["project_path"] == str(tmp_path)
        assert listed["sessions"][0]["title"] == "My chat"
        assert listed["sessions"][0]["running"] is False
        assert await request("session.delete", {"session_id": sid}) == {"deleted": True}
        assert (await request("session.list", {}))["sessions"] == []
    finally:
        writer.close()
        await writer.wait_closed()
        await server.stop()


# 功能：验证自定义模型服务仅列出自己的当前模型且不会泄露 API 密钥
# 设计：以测试环境变量模拟兼容服务，检查完整序列化结果并确认原生服务仍给出候选
async def test_model_catalog_respects_provider_and_excludes_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = CoreApp()
    app._config = MiniConfig()
    app._config.llm.default_model = "custom-model"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid/anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret-must-not-leak")
    catalog = await app._config_models_handler({})
    assert [model.id for model in catalog.models] == ["custom-model"]
    assert "test-secret" not in catalog.model_dump_json()
    assert "example.invalid" not in catalog.model_dump_json()
    monkeypatch.delenv("ANTHROPIC_BASE_URL")
    app._config.llm.default_model = "claude-sonnet-4-6"
    assert len((await app._config_models_handler({})).models) > 1


# 功能：验证事件历史回放不会泄露其他项目的运行文件
# 设计：在共享会话存储中创建两个真实事件文件，检查当前项目仅定位自身记录
async def test_replay_paths_are_project_scoped(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    first = manager_for(root, tmp_path / "one")
    second = manager_for(root, tmp_path / "two")
    own = await first.create("chat")
    other = await second.create("chat")
    own_run = await first.send_message(own.id, "own")
    other_run = await second.send_message(other.id, "private")
    assert first.events_path(own_run) == root / own.id / "runs" / own_run / "events.jsonl"
    assert first.events_path(other_run) is None


# 功能：验证删除会话等待后台清理时不能并发开始新运行
# 设计：人为暂停真实删除路径的后台清理点，确保锁覆盖删除前的整个等待窗口
async def test_delete_holds_session_lock_during_background_cleanup(tmp_path: Path) -> None:
    manager = manager_for(tmp_path / "sessions", tmp_path)
    session = await manager.create("chat")
    cleaning = asyncio.Event()
    release = asyncio.Event()

    class BackgroundRunner:
        # 暂停后台清理以暴露删除与发送消息之间的并发窗口
        async def cancel_background(self) -> bool:
            cleaning.set()
            await release.wait()
            return True

    manager._runners[session.id] = [BackgroundRunner()]
    deleting = asyncio.create_task(manager.delete(session.id))
    await asyncio.wait_for(cleaning.wait(), 3)
    with pytest.raises(HandlerError, match="busy"):
        await manager.send_message(session.id, "must not run")
    release.set()
    await deleting
    assert not (tmp_path / "sessions" / session.id).exists()
