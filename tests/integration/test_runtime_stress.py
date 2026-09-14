from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from mini_claude.core.app import CoreApp
from mini_claude.core.config import MiniConfig
from mini_claude.core.llm.types import LlmResponse, ToolCallBlock
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.runner import AgentRunner
from mini_claude.core.session.manager import SessionManager
from mini_claude.core.session.store import SessionStore
from mini_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from mini_claude.core.transport.socket_client import SocketClient
from mini_claude.core.transport.socket_server import SocketServer


@pytest.fixture(autouse=True)
# 隔离单独运行时的用户和项目上下文，模型替身无需加载真实配置内容
def isolated_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mini_claude.core.runner.load_context_file", lambda path: "")


class SideEffectProvider:
    # 每个会话复用工具调用 ID，执行一次可观察副作用后失败
    async def chat(self, *, messages: Any, step: int, **kwargs: Any) -> LlmResponse:
        owner = messages[0]["content"]
        if step == 1:
            return LlmResponse(stop_reason="tool_use", tool_calls=[ToolCallBlock(
                id="shared-id", name="bash",
                input={"command": f"printf '{owner}\\n' >> effects.log; exit 1"},
            )])
        return LlmResponse(stop_reason="end_turn", text=f"FINAL {owner}")


@asynccontextmanager
# 启动真实 Core 与两个独立 TCP 客户端，仅替换外部模型并隔离存储
async def runtime_pair(path: Path) -> AsyncIterator[tuple[CoreApp, list[SocketClient]]]:
    core = CoreApp()
    core._broadcaster = IpcEventBroadcaster()
    core._bus.subscribe(core._broadcaster.handle)
    core._permission_manager = PermissionManager(policy_file=path / "policy.toml", timeout_s=0)
    config = MiniConfig()
    config.network.enabled = False
    core._sessions = SessionManager(
        SessionStore(path / "sessions"),
        lambda: AgentRunner(config, provider=SideEffectProvider(), bus=core._bus,
                            permission_manager=core._permission_manager),
        core._bus, project_path=path, permission_manager=core._permission_manager,
    )
    server = SocketServer("127.0.0.1", 0, broadcaster=core._broadcaster)
    for name, handler in [
        ("event.subscribe", core._subscribe_handler),
        ("session.create", core._session_create_handler),
        ("session.send_message", core._session_send_handler),
        ("session.cancel", core._session_cancel_handler),
        ("session.close", core._session_close_handler),
        ("session.delete", core._session_delete_handler),
        ("permission.respond", core._permission_respond_handler),
    ]:
        server.register(name, handler)
    clients: list[SocketClient] = []
    loops: list[asyncio.Task[Any]] = []
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    try:
        for _ in range(2):
            client = SocketClient("127.0.0.1", port)
            await client.connect()
            clients.append(client)
            loops.append(asyncio.create_task(client.run_event_loop()))
            await client.send_command("event.subscribe", {"topics": ["*"], "scope": "global"})
        yield core, clients
    finally:
        await core._sessions.stop_all()
        for client in clients:
            await client.close()
        for loop in loops:
            loop.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        await server.stop()


# 功能：多个会话同时申请同 ID 审批，双客户端相反决定只接受一次且副作用次数准确
# 设计：走真实 RPC、审批、Bash 和存储，按服务端接受事件核对实际文件而不假定竞争赢家
@pytest.mark.parametrize("count", [2, 12])
async def test_competing_approvals_execute_only_accepted_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int,
) -> None:
    monkeypatch.chdir(tmp_path)
    async with runtime_pair(tmp_path) as (core, clients), asyncio.timeout(10):
        requested: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        events: list[dict[str, Any]] = []

        # 保留单个客户端观察到的事件顺序并等待全部审批进入挂起状态
        async def receive(event: dict[str, Any]) -> None:
            events.append(event)
            if event["type"] == "permission.requested":
                requested.put_nowait(event)

        clients[0].on_event(receive)
        sessions = [await clients[0].send_command("session.create", {"mode": "chat"})
                    for _ in range(count)]
        owners = {session["session_id"]: f"OWNER-{i}" for i, session in enumerate(sessions)}
        runs = [asyncio.create_task(clients[i % 2].send_command("session.send_message", {
            "session_id": sid, "content": owner,
        })) for i, (sid, owner) in enumerate(owners.items())]
        try:
            pending = [await requested.get() for _ in range(count)]
            replies = []
            for index, event in enumerate(pending):
                decisions = ("allow_once", "deny_once") if index % 2 else ("deny_once", "allow_once")
                for client, decision in zip(clients, decisions):
                    replies.append(client.send_command("permission.respond", {
                        "tool_use_id": event["tool_use_id"], "run_id": event["run_id"],
                        "decision": decision,
                    }))
            await asyncio.gather(*replies)
            await asyncio.gather(*runs)
            # 同一连接的 RPC 回复屏障确保此前推送已经分发到本地事件处理器
            await clients[0].send_command("event.subscribe", {"topics": [], "scope": "global"})
            decisions = [event for event in events if event["type"] in
                         ("permission.granted", "permission.denied")]
            assert len(decisions) == count
            assert len({event["run_id"] for event in decisions}) == count
            expected = Counter(owners[event["session_id"]] for event in decisions
                               if event["type"] == "permission.granted")
            effects = tmp_path / "effects.log"
            assert Counter(effects.read_text().splitlines() if effects.exists() else []) == expected
            assert core._sessions is not None
            assert core._permission_manager is not None
            for sid, owner in owners.items():
                assert core._permission_manager.pending_for(sid) == []
                history = await core._sessions.get_history(sid)
                assert history[-1]["content"] == [{"type": "text", "text": f"FINAL {owner}"}]
                assert len(history) == 4
                run_id = core._sessions._get_session(sid).run_ids[0]
                log = core._sessions.events_path(run_id)
                assert log is not None
                rows = [json.loads(line) for line in log.read_text().splitlines()]
                assert all(row["session_id"] == sid and row["run_id"] == run_id for row in rows)
                assert sum(row["type"] == "tool.call_failed" for row in rows) == 1
        finally:
            for task in runs:
                task.cancel()
            await asyncio.gather(*runs, return_exceptions=True)


# 功能：审批挂起时取消、关闭或删除一个会话，另一个会话仍能获批执行
# 设计：两个客户端跨会话发送生命周期命令，迟到批准不得复活已取消的 Bash
@pytest.mark.parametrize("action", ["cancel", "close", "delete"])
async def test_lifecycle_during_approval_does_not_revive_or_cross_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    async with runtime_pair(tmp_path) as (core, clients), asyncio.timeout(10):
        requested: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # 用请求事件确认生命周期命令发生在工具尚未执行的审批窗口
        async def receive(event: dict[str, Any]) -> None:
            if event["type"] == "permission.requested":
                requested.put_nowait(event)

        clients[0].on_event(receive)
        a, b = [await clients[0].send_command("session.create", {"mode": "chat"}) for _ in range(2)]
        runs = [asyncio.create_task(client.send_command("session.send_message", {
            "session_id": session["session_id"], "content": owner,
        })) for client, session, owner in zip(clients, (a, b), ("A", "B"))]
        try:
            pending = [await requested.get(), await requested.get()]
            own = next(event for event in pending if event["session_id"] == a["session_id"])
            other = next(event for event in pending if event["session_id"] == b["session_id"])
            await clients[1].send_command(f"session.{action}", {"session_id": a["session_id"]})
            assert core._permission_manager is not None
            assert not core._permission_manager.pending_for(a["session_id"])
            assert core._permission_manager.pending_for(b["session_id"])
            assert not runs[1].done()
            for event in (own, other):
                await clients[0].send_command("permission.respond", {
                    "run_id": event["run_id"], "tool_use_id": event["tool_use_id"],
                    "decision": "allow_once",
                })
            outcomes = await asyncio.gather(*runs, return_exceptions=True)
            assert not isinstance(outcomes[1], BaseException)
            assert (tmp_path / "effects.log").read_text() == "B\n"
            assert core._sessions is not None
            assert (await core._sessions.get_history(b["session_id"]))[-1]["content"] == [
                {"type": "text", "text": "FINAL B"},
            ]
            if action == "delete":
                assert not core._sessions._store.session_dir(a["session_id"]).exists()
            else:
                history = await core._sessions.get_history(a["session_id"])
                assert history[-1]["content"][0]["is_error"] is True
                assert "interrupted" in history[-1]["content"][0]["content"].lower()
        finally:
            for task in runs:
                task.cancel()
            await asyncio.gather(*runs, return_exceptions=True)
