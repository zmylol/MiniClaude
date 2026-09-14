from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from mini_claude.core.app import CoreApp
from mini_claude.core.bus.events import LlmTokenEvent
from mini_claude.core.config import MiniConfig
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.types import LlmResponse, ToolCallBlock
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.runner import AgentRunner
from mini_claude.core.session.manager import SessionManager
from mini_claude.core.session.store import SessionStore
from mini_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from mini_claude.core.transport.socket_client import SocketClient
from mini_claude.core.transport.socket_server import SocketServer
from mini_claude.tui.app import ChatTextArea, LLMStreamBlock, MiniTuiApp, PermissionSelect


# 功能：真实双客户端交错审批、输出和关闭时保持会话、界面和日志隔离
# 设计：只替换外部模型，使用真实 Core/RPC/Runner/Bash/存储以及两个 Textual DOM
@pytest.mark.parametrize("real_startup", [False, True])
async def test_two_clients_share_core_without_crossing_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_startup: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    if not real_startup:
        monkeypatch.setattr(MiniTuiApp, "on_mount", lambda self: None)

    class Provider:
        # 固定回复与局部流式文本，工具故意追加后失败来检查执行次数
        async def chat(
            self, *, messages: Any, bus: EventBus, run_id: str, step: int, **kwargs: Any,
        ) -> LlmResponse:
            if run_id == "compact":
                return LlmResponse(stop_reason="end_turn", text="COMPACT SUMMARY")
            name = messages[0]["content"]
            await bus.publish(LlmTokenEvent(
                run_id=run_id, step=step, token=f"PARTIAL {name}", ts="t",
            ))
            if step == 1:
                return LlmResponse(stop_reason="tool_use", text=f"CHECK {name}", tool_calls=[
                    ToolCallBlock(id="same-tool-id", name="bash", input={
                        "command": f"printf '{name}\\n' >> effects.log; exit 1",
                    }),
                ])
            return LlmResponse(stop_reason="end_turn", text=f"FINAL {name}")

    core = CoreApp()
    broadcaster = IpcEventBroadcaster()
    core._broadcaster = broadcaster
    core._bus.subscribe(broadcaster.handle)
    permissions = PermissionManager(policy_file=tmp_path / "policy.toml", timeout_s=0)
    core._permission_manager = permissions
    config = MiniConfig()
    config.network.enabled = False
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store, lambda: AgentRunner(config, provider=Provider(), bus=core._bus,
                                   permission_manager=permissions),
        core._bus, project_path=tmp_path, permission_manager=permissions, provider=Provider(),
    )
    core._sessions = manager
    server = SocketServer("127.0.0.1", 0, broadcaster=broadcaster)
    for name, handler in [
        ("event.subscribe", core._subscribe_handler),
        ("session.create", core._session_create_handler),
        ("session.send_message", core._session_send_handler),
        ("session.get_history", core._session_history_handler),
        ("session.close", core._session_close_handler),
        ("session.compact", core._session_compact_handler),
        ("permission.respond", core._permission_respond_handler),
    ]:
        server.register(name, handler)
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    clients: list[SocketClient] = []
    loops: list[asyncio.Task[Any]] = []
    runs: list[asyncio.Task[Any]] = []
    requested: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    a, b = MiniTuiApp("127.0.0.1", port), MiniTuiApp("127.0.0.1", port)
    try:
        async with a.run_test() as first_pilot, b.run_test() as second_pilot:
            for ui in (a, b):
                if real_startup:
                    async with asyncio.timeout(3):
                        while ui._session_id is None or ui._client is None:
                            await asyncio.sleep(0.01)
                    client = ui._client
                else:
                    client = SocketClient("127.0.0.1", port)
                    await client.connect()
                    loops.append(asyncio.create_task(client.run_event_loop()))
                    created = await client.send_command("session.create", {"mode": "chat"})
                    ui._session_id = created["session_id"]
                    ui._client = client
                clients.append(client)

                # 向真实界面分发来自 TCP 的事件，保留全局订阅以检验前端隔离
                async def receive(event: dict[str, Any], target: MiniTuiApp = ui) -> None:
                    if not real_startup:
                        target._handle_event(event)
                    if target is a and event["type"] == "permission.requested":
                        requested.put_nowait(event)

                client.on_event(receive)
                if not real_startup:
                    await client.send_command("event.subscribe", {
                        "topics": ["*"], "scope": "global",
                    })
            for ui, client, name in zip((a, b), clients, ("A", "B")):
                runs.append(asyncio.create_task(client.send_command("session.send_message", {
                    "session_id": ui._session_id, "content": name,
                })))
            approvals = [await asyncio.wait_for(requested.get(), 3) for _ in range(2)]
            own = next(event for event in approvals if event["session_id"] == a._session_id)
            other = next(event for event in approvals if event["session_id"] == b._session_id)
            await first_pilot.pause()
            await second_pilot.pause()
            assert len(a.query(PermissionSelect)) == len(b.query(PermissionSelect)) == 1
            await clients[1].send_command("permission.respond", {
                "tool_use_id": own["tool_use_id"], "run_id": own["run_id"],
                "decision": "allow_once",
            })
            await asyncio.wait_for(runs[0], 3)
            await first_pilot.pause()
            await second_pilot.pause()
            assert len(a.query(PermissionSelect)) == 0
            assert len(b.query(PermissionSelect)) == 1
            assert not a.query_one("#prompt", ChatTextArea).disabled
            assert b.query_one("#prompt", ChatTextArea).disabled
            await clients[0].send_command("session.close", {"session_id": a._session_id})
            assert not runs[1].done()
            await clients[0].send_command("permission.respond", {
                "tool_use_id": other["tool_use_id"], "run_id": other["run_id"],
                "decision": "deny_once",
            })
            await asyncio.wait_for(runs[1], 3)
            await second_pilot.pause()
            assert (tmp_path / "effects.log").read_text() == "A\n"
            for ui, name, forbidden in [(a, "A", "B"), (b, "B", "A")]:
                text = "\n".join(block._text for block in ui.query(LLMStreamBlock))
                assert f"FINAL {name}" in text
                assert "PARTIAL" not in text
                assert f"FINAL {forbidden}" not in text
                assert ui._session_id is not None
                history = await manager.get_history(ui._session_id)
                assert f"FINAL {name}" in json.dumps(history)
                run_id = manager._get_session(ui._session_id).run_ids[0]
                events = (store.runs_dir(ui._session_id) / run_id / "events.jsonl").read_text()
                assert all(json.loads(line)["run_id"] == run_id for line in events.splitlines())
            if real_startup:
                before = await manager.get_history(b._session_id)
                await b._do_compact()
                assert await manager.get_history(b._session_id) == before
                assert store.read_messages(b._session_id)[0]["content"] == "COMPACT SUMMARY"
    finally:
        await manager.stop_all()
        for task in runs:
            task.cancel()
        await asyncio.gather(*runs, return_exceptions=True)
        for client in clients:
            await client.close()
        for loop in loops:
            loop.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        await server.stop()
