from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from mini_claude.core.bus.events import LlmTokenEvent
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.provider import AnthropicProvider
from mini_claude.core.llm.types import LlmResponse
from tests.unit.test_history_regressions import manager_for
from tests.unit.test_llm_provider import FakeStream, _make_final
from tests.unit.test_runtime_regressions import BackgroundProvider


# 功能：含 Unicode 换行和段落分隔符的正式响应在日志回放中仍完整可见
# 设计：通过真实运行器写事件文件再调用 Core 回放，检查 JSONL 只按文件换行拆记录
async def test_replay_preserves_unicode_separators_in_response_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from mini_claude.core.app import CoreApp

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mini_claude.core.runner.load_context_file", lambda path: "")
    answer = "before\u2028middle\u2029next\u0085after"
    provider = SimpleNamespace(chat=AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn", text=answer,
    )))
    manager = manager_for(tmp_path, provider)
    session = await manager.create("chat")
    await manager.send_message(session.id, "GOAL", run_id="unicode-run")
    path = manager.events_path("unicode-run")
    assert path is not None
    with path.open("ab") as stream:
        stream.write(b'{"type":"llm.token","token":"\xe4\xb8')
    core = CoreApp()
    core._sessions = manager
    writer = MagicMock()
    writer.drain = AsyncMock()
    await core._replay_events("unicode-run", writer, ["llm.response.completed"])
    replay = [json.loads(call.args[0])["event"] for call in writer.write.call_args_list]
    assert [event["text"] for event in replay] == [answer]


# 功能：真实断流重试后发布完整响应事件，携带和临时 token 相同的步骤标识
# 设计：在 SDK 流替身注入半句后 ReadError，经真实 provider/loop/存储验收最终文本
@pytest.mark.parametrize("exhausted", [False, True])
async def test_stream_retry_has_authoritative_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exhausted: bool,
) -> None:
    class BrokenStream(FakeStream):
        @property
        # 输出半句后模拟网络断流
        def text_stream(self) -> Any:
            # 迭代器抛错以触发实际重试逻辑
            async def chunks() -> Any:
                yield "INCOMPLETE"
                raise httpx.ReadError("disconnected")
            return chunks()

    monkeypatch.setattr("mini_claude.core.llm.provider._RETRY_BACKOFF_S", (0, 0, 0))
    client = MagicMock()
    broken = BrokenStream([], _make_final())
    client.messages.stream.side_effect = (
        [broken, broken, broken] if exhausted else
        [broken, FakeStream(["COMPLETE ANSWER"], _make_final())]
    )
    bus = EventBus()
    received: list[Any] = []

    # 保存从生产事件总线发出的协议数据
    async def collect(event: Any) -> None:
        received.append(event.model_dump())

    bus.subscribe(collect)
    manager = manager_for(tmp_path, AnthropicProvider("test", client=client), bus)
    session = await manager.create("chat")
    await manager.send_message(session.id, "GOAL", run_id="response-run")
    terminal_type = "llm.response.failed" if exhausted else "llm.response.completed"
    terminal = [event for event in received if event["type"] == terminal_type]
    assert len(terminal) == 1
    assert terminal[0]["step"] == 1
    assert terminal[0]["session_id"] == session.id
    token = next(event for event in received if event["type"] == "llm.token")
    assert token["step"] == terminal[0]["step"]
    assert token["root_run_id"] == "response-run"
    if not exhausted:
        assert terminal[0]["text"] == "COMPLETE ANSWER"
        assert terminal[0]["text"] in json.dumps(await manager.get_history(session.id))
    else:
        assert terminal[0]["reason"] == "llm_error"
        assert "INCOMPLETE" not in json.dumps(await manager.get_history(session.id))


# 功能：并发会话日志仅记录自身运行，后台子代理结束后仍有独立完整日志和归属
# 设计：共享生产总线并交错两个 run，随后让第一轮后台任务在主任务结束后才完成
async def test_run_logs_and_background_scope_are_isolated(tmp_path: Path) -> None:
    bus = EventBus()
    release = asyncio.Event()
    started = asyncio.Event()

    class Provider:
        # 用信号确保两个运行日志订阅同时存在
        async def chat(self, *, bus: EventBus, run_id: str, **kwargs: Any) -> LlmResponse:
            if run_id == "a":
                started.set()
                await release.wait()
            await bus.publish(LlmTokenEvent(run_id=run_id, token=f"TEXT {run_id}", ts="t"))
            return LlmResponse(stop_reason="end_turn", text=f"TEXT {run_id}")

    manager = manager_for(tmp_path, Provider(), bus)
    a = await manager.create("chat")
    b = await manager.create("chat")
    baseline = len(bus._subscribers)
    first = asyncio.create_task(manager.send_message(a.id, "A", run_id="a"))
    await asyncio.wait_for(started.wait(), 2)
    await manager.send_message(b.id, "B", run_id="b")
    release.set()
    await first
    for session, run_id in [(a, "a"), (b, "b")]:
        path = manager._store.runs_dir(session.id) / run_id / "events.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert all(event.get("run_id") == run_id for event in records)
        assert all(event["session_id"] == session.id for event in records)
    assert len(bus._subscribers) == baseline

    background = BackgroundProvider()
    child_manager = manager_for(tmp_path / "child", background, bus)
    session = await child_manager.create("chat")
    try:
        await child_manager.send_message(session.id, "SPAWN", run_id="first")
        await asyncio.wait_for(background.started.wait(), 2)
        background.release.set()
        await asyncio.wait_for(background.finished.wait(), 2)
        await asyncio.sleep(0)
        path = child_manager._store.runs_dir(session.id) / background.child_id / "events.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert records[0]["type"] == "subagent.started"
        assert records[-1]["type"] == "subagent.finished"
        assert all(event["session_id"] == session.id for event in records)
        assert all(event["root_run_id"] == "first" for event in records)
    finally:
        await child_manager.stop_all()


# 功能：回放包含关联子日志并过滤旧日志混入的其他运行
# 设计：写入带旧格式混写记录的根日志和独立子日志，通过生产回放入口检查输出
async def test_replay_filters_foreign_and_includes_children(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock

    from mini_claude.core.app import CoreApp

    manager = manager_for(tmp_path, MagicMock())
    session = await manager.create("chat")
    session.run_ids.append("root")
    rows = {
        "root": [
            {"type": "run.started", "run_id": "root", "session_id": session.id, "ts": "1"},
            {"type": "subagent.started", "run_id": "child", "parent_run_id": "root", "ts": "2"},
            {"type": "llm.token", "run_id": "foreign", "token": "PRIVATE", "ts": "3"},
        ],
        "child": [
            {"type": "llm.token", "run_id": "child", "parent_run_id": "root",
             "root_run_id": "root", "session_id": session.id, "token": "CHILD", "ts": "4"},
        ],
    }
    for run, events in rows.items():
        path = manager._store.runs_dir(session.id) / run / "events.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
    app = CoreApp()
    app._sessions = manager
    writer = MagicMock()
    writer.drain = AsyncMock()
    await app._replay_events("root", writer, ["*"])
    replay = [json.loads(call.args[0])["event"] for call in writer.write.call_args_list]
    assert "PRIVATE" not in json.dumps(replay)
    assert "CHILD" in json.dumps(replay)
    writer.write.reset_mock()
    await app._replay_events("root", writer, ["*"], scope="run:foreign")
    writer.write.assert_not_called()


# 功能：旧日志缺少完整归属字段时，运行树与会话回放仍包含正常子代理进度
# 设计：仅以旧 started 记录关联父子运行，混入外部 token 并从生产回放入口检查过滤结果
@pytest.mark.parametrize("scope_kind", ["tree", "session"])
async def test_scoped_replay_recovers_legacy_child_ownership(
    tmp_path: Path, scope_kind: str,
) -> None:
    from unittest.mock import AsyncMock

    from mini_claude.core.app import CoreApp

    manager = manager_for(tmp_path, MagicMock())
    session = await manager.create("chat")
    session.run_ids.append("root")
    rows = {
        "root": [
            {"type": "run.started", "run_id": "root", "session_id": session.id, "ts": "1"},
            {"type": "subagent.started", "run_id": "child", "parent_run_id": "root", "ts": "2"},
            {"type": "llm.token", "run_id": "foreign", "token": "PRIVATE", "ts": "3"},
        ],
        "child": [
            {"type": "llm.token", "run_id": "child", "token": "CHILD", "ts": "4"},
            {"type": "subagent.finished", "run_id": "child", "parent_run_id": "root",
             "status": "success", "ts": "5"},
        ],
    }
    for run, events in rows.items():
        path = manager._store.runs_dir(session.id) / run / "events.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
    app = CoreApp()
    app._sessions = manager
    writer = MagicMock()
    writer.drain = AsyncMock()
    scope = "tree:root" if scope_kind == "tree" else f"session:{session.id}"
    await app._replay_events("root", writer, ["*"], scope=scope)
    replay = [json.loads(call.args[0])["event"] for call in writer.write.call_args_list]
    assert [event["type"] for event in replay] == [
        "run.started", "subagent.started", "llm.token", "subagent.finished",
    ]
    assert "PRIVATE" not in json.dumps(replay)
    assert "CHILD" in json.dumps(replay)


# 功能：两个会话使用相同工具调用 ID 时审批互不覆盖，重复决定不反转结果
# 设计：同时挂起真实权限管理器请求，按运行 ID 定向批准与拒绝
async def test_permission_ids_are_scoped_to_run() -> None:
    from mini_claude.core.permissions.manager import PermissionManager

    manager = PermissionManager(timeout_s=0)
    requested = asyncio.Queue()

    # 用请求通知精确等待两个审批进入挂起状态
    async def emit(event: Any) -> None:
        await requested.put(event)

    first = asyncio.create_task(manager.check_and_wait(
        "same", "unknown", {}, "sess-a", emit, run_id="a",
    ))
    second = asyncio.create_task(manager.check_and_wait(
        "same", "unknown", {}, "sess-b", emit, run_id="b",
    ))
    try:
        await requested.get()
        await requested.get()
        assert len(manager.pending_for("sess-a")) == 1
        assert len(manager.pending_for("sess-b")) == 1
        manager.respond("same", "allow_once", run_id="a")
        manager.respond("same", "deny_once", run_id="a")
        assert await asyncio.wait_for(first, 2) == (True, "allow_once")
        assert not second.done()
        manager.respond("same", "deny_once", run_id="b")
        assert await asyncio.wait_for(second, 2) == (False, "deny_once")
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
