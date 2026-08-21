from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mini_claude.core.bus.envelope import HandlerError
from mini_claude.core.events.bus import EventBus
from mini_claude.core.runner import RunOutcome
from mini_claude.core.session.manager import SESSION_CLOSED, SESSION_NOT_FOUND, SessionManager
from mini_claude.core.session.model import Session
from mini_claude.core.session.store import SessionStore


class _Runner:
    # 模拟 AgentRunner，将 run 新消息写入 thread 后返回成功
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        assert run_id is not None
        assert session is not None
        assert store is not None
        store.append_messages(
            session.id,
            [{"role": "assistant", "content": [{"type": "text", "text": f"done {goal}"}]}],
            run_id,
        )
        return RunOutcome(status="success", result="done", reason=None)


class _BlockingRunner:
    # 初始化可由测试观察启动状态的阻塞 runner
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self._forever = asyncio.Event()

    # 模拟持续运行的 AgentRunner，直到测试通过 cancel_run 发出取消信号
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        self.started.set()
        await self._forever.wait()
        return RunOutcome(status="success", result="unreachable", reason=None)


class _PermissionManager:
    # 记录取消 run 时 SessionManager 是否同步释放了该 session 的待审批请求
    def __init__(self) -> None:
        self.cancelled_sessions: list[tuple[str, str]] = []

    # 模拟 PermissionManager.cancel_session，并保存 session_id 与取消原因供断言
    def cancel_session(self, session_id: str, reason: str = "client_disconnected") -> None:
        self.cancelled_sessions.append((session_id, reason))


# 功能：验证 create 会创建 active session、写入 meta 并发布 session.created 事件
# 设计：用真实 SessionStore + EventBus 收集事件，覆盖 manager 与 store/bus 的协作边界
async def test_create_session_writes_meta_and_event(tmp_path: Path) -> None:
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]

    session = await manager.create("chat", "title")

    assert session.status == "active"
    assert store.read_meta(session.id).title == "title"
    assert [e.type for e in events] == ["session.created"]  # type: ignore[attr-defined]


# 功能：验证 chat session 处理一条消息后进入 waiting_for_input，并保留 user/assistant thread
# 设计：mock runner 主动追加 assistant 消息，确认 send_message 负责 user 消息、状态流转和 run_id 记录
async def test_send_message_chat_enters_waiting_and_writes_thread(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")

    run_id = await manager.send_message(session.id, "hello")

    loaded = store.read_meta(session.id)
    assert loaded.status == "waiting_for_input"
    assert loaded.run_ids == [run_id]
    messages = store.read_messages(session.id)
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[1]["role"] == "assistant"


# 功能：验证 one_shot session 在单次消息完成后自动 closed
# 设计：复用 mock runner 的成功路径，聚焦 mode 对最终状态的影响，保证 mini run 的统一路径正确
async def test_one_shot_auto_closes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("one_shot")

    await manager.send_message(session.id, "hello")

    assert store.read_meta(session.id).status == "closed"


# 功能：验证不存在的 session_id 返回 session_not_found 错误码
# 设计：直接调用 get_history 的查找路径，断言 HandlerError code，覆盖 IPC handler 可结构化返回错误
async def test_missing_session_raises_handler_error(tmp_path: Path) -> None:
    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    with pytest.raises(HandlerError) as exc:
        await manager.get_history("missing")
    assert exc.value.code == SESSION_NOT_FOUND


# 功能：验证 closed session 不能继续 send_message
# 设计：先显式 close，再发送消息，断言 session_closed 错误码，覆盖状态机拒绝路径
async def test_closed_session_rejects_message(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    await manager.close(session.id)

    with pytest.raises(HandlerError) as exc:
        await manager.send_message(session.id, "again")
    assert exc.value.code == SESSION_CLOSED


# 功能：验证 cancel_run 会终止活动 runner，并让 chat session 恢复为可继续输入状态
# 设计：用永不主动结束的 runner 暴露取消边界，断言 RPC 外层正常返回且运行索引被清理
async def test_cancel_run_restores_chat_session(tmp_path: Path) -> None:
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    runner = _BlockingRunner()
    permissions = _PermissionManager()
    store = SessionStore(tmp_path)
    manager = SessionManager(
        store,
        lambda: runner,
        bus,
        permission_manager=permissions,  # type: ignore[arg-type]
    )  # type: ignore[arg-type]
    session = await manager.create("chat")
    send_task = asyncio.create_task(
        manager.send_message(session.id, "keep working", run_id="run-cancel")
    )
    await runner.started.wait()

    assert manager.cancel_run("run-cancel")
    assert await send_task == "run-cancel"
    assert store.read_meta(session.id).status == "waiting_for_input"
    assert events[-1].type == "session.waiting_for_input"  # type: ignore[attr-defined]
    assert permissions.cancelled_sessions == [(session.id, "run_cancelled")]
    assert not manager.cancel_run("run-cancel")
