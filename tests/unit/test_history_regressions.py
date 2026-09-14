from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from mini_claude.core.config import MiniConfig
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from mini_claude.core.runner import AgentRunner
from mini_claude.core.session.manager import SessionManager
from mini_claude.core.session.store import SessionStore


class CompactingProvider:
    # 按指定工具轮数触发压缩，并记录下一次模型实际收到的上下文
    def __init__(self, tool_steps: int = 1, cancel: bool = False) -> None:
        self.tool_steps = tool_steps
        self.cancel = cancel
        self.summaries = 0
        self.inputs: list[Any] = []

    # 模拟摘要、工具调用、最终回复和压缩后取消而不连接外部模型
    async def chat(self, *, messages: Any, run_id: str, step: int = 0, **kwargs: Any) -> LlmResponse:
        if run_id == "compact":
            self.summaries += 1
            return LlmResponse(stop_reason="end_turn", text=f"SUMMARY {self.summaries}")
        self.inputs.append(copy.deepcopy(messages))
        if step <= self.tool_steps:
            return LlmResponse(
                stop_reason="tool_use", text=f"BEFORE COMPACTION {step}",
                tool_calls=[ToolCallBlock(id=f"call-{step}", name="missing", input={})],
                usage=UsageStats(input_tokens=100, output_tokens=10, context_pct=0.9),
            )
        if self.cancel:
            raise asyncio.CancelledError
        return LlmResponse(stop_reason="end_turn", text="FINAL ANSWER")


# 构造真实会话与运行器，仅替换模型和关闭联网工具
def manager_for(path: Path, provider: Any, bus: EventBus | None = None) -> SessionManager:
    config = MiniConfig()
    config.network.enabled = False
    config.compaction.auto_threshold = 0.8
    event_bus = bus or EventBus()
    return SessionManager(
        SessionStore(path),
        lambda: AgentRunner(config, provider=provider, bus=event_bus, runs_dir=path / "runs"),
        event_bus, provider=provider, project_path=path,
    )


# 功能：一次或多次自动压缩均保留原始消息，重启后模型继续使用最新摘要
# 设计：真实 manager→runner→compactor→store 链路，按消息内容和工具配对验收
@pytest.mark.parametrize("count", [1, 2])
async def test_compaction_keeps_history_and_restores_context(tmp_path: Path, count: int) -> None:
    provider = CompactingProvider(count)
    manager = manager_for(tmp_path, provider)
    session = await manager.create("chat")
    for index in range(5):
        manager._store.append_message(session.id, "user", f"OLD {index}")
    await manager.send_message(session.id, "GOAL")
    history = await manager.get_history(session.id)
    assert len(history) == 6 + count * 2 + 1
    assert history[-1]["content"] == [{"type": "text", "text": "FINAL ANSWER"}]
    assert sum("tool_result" in json.dumps(message) for message in history) == count
    assert "SUMMARY" not in json.dumps(history)

    restarted_provider = CompactingProvider(0)
    restarted = manager_for(tmp_path, restarted_provider)
    await restarted.send_message(session.id, "NEXT")
    model_input = restarted_provider.inputs[0]
    assert model_input[0]["content"] == f"SUMMARY {count}"
    assert "OLD 0" not in json.dumps(model_input)
    assert "FINAL ANSWER" in json.dumps(model_input)
    assert model_input[-1]["content"] == "NEXT"
    assert len(await restarted.get_history(session.id)) == len(history) + 2


# 功能：压缩后的取消不能丢失压缩前工具过程
# 设计：在压缩后下一次模型调用抛取消，重新读取磁盘检查消息完整且没有摘要混入
async def test_cancel_after_compaction_keeps_tool_messages(tmp_path: Path) -> None:
    manager = manager_for(tmp_path, CompactingProvider(2, cancel=True))
    session = await manager.create("chat")
    with pytest.raises(asyncio.CancelledError):
        await manager.send_message(session.id, "GOAL")
    history = await manager.get_history(session.id)
    assert len(history) == 5
    assert "BEFORE COMPACTION 1" in json.dumps(history)
    assert "BEFORE COMPACTION 2" in json.dumps(history)
    assert "SUMMARY" not in json.dumps(history)


# 功能：运行完成通知出现时回复已经保存，通知异常不能阻止落盘
# 设计：订阅真实事件总线并在完成事件时读取磁盘，随后模拟客户端通知失败
async def test_finished_event_observes_committed_history(tmp_path: Path) -> None:
    bus = EventBus()
    manager = manager_for(tmp_path, CompactingProvider(0), bus)
    session = await manager.create("chat")
    observed: list[Any] = []

    # 在完成通知回调中观察已经提交的历史
    async def collect(event: Any) -> None:
        if event.type == "run.finished":
            observed.extend(await manager.get_history(session.id))
            raise RuntimeError("notification disconnected")

    bus.subscribe(collect)
    try:
        await manager.send_message(session.id, "GOAL")
    except RuntimeError:
        pass
    assert "FINAL ANSWER" in json.dumps(observed)
    assert "FINAL ANSWER" in json.dumps(await manager.get_history(session.id))


# 功能：手动压缩不覆盖界面历史，长工具结果和未配对调用也能查看
# 设计：先保存真实长结果，再手动压缩并重建 manager，检查展示与模型读取分离
async def test_manual_compaction_preserves_raw_history(tmp_path: Path) -> None:
    manager = manager_for(tmp_path, CompactingProvider(0))
    session = await manager.create("chat")
    long_result = "LONG RESULT " * 5000
    store = manager._store
    store.append_message(session.id, "user", "GOAL")
    store.append_message(session.id, "assistant", [
        {"type": "tool_use", "id": "t", "name": "read_file", "input": {}},
    ])
    store.append_message(session.id, "user", [
        {"type": "tool_result", "tool_use_id": "t", "content": long_result},
    ])
    before = (store.session_dir(session.id) / "thread.jsonl").read_bytes()
    await manager.compact(session.id)
    assert (store.session_dir(session.id) / "thread.jsonl").read_bytes() == before
    assert (await manager.get_history(session.id))[-1]["content"][0]["content"] == long_result
    assert store.read_messages(session.id)[0]["content"] == "SUMMARY 1"
    store.append_message(session.id, "assistant", [
        {"type": "tool_use", "id": "orphan", "name": "read_file", "input": {}},
    ])
    assert len(await manager.get_history(session.id)) == 4


# 功能：历史或摘要提交失败仍清理浏览器，并向客户端报告失败而非成功
# 设计：在生产存储接口注入磁盘错误，观察清理资源和运行终态两个出口
@pytest.mark.parametrize("failure", ["history", "checkpoint"])
async def test_storage_failure_cleans_browser_and_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from mini_claude.core.session.model import Session

    config = MiniConfig()
    config.compaction.auto_threshold = 0.8
    browser = MagicMock()
    browser.get_tools.return_value = []
    browser.close = AsyncMock()
    monkeypatch.setattr("mini_claude.core.runner.BrowserSession", lambda **kwargs: browser)
    store = SessionStore(tmp_path)
    session = Session(id="sess-failure", mode="chat", status="active", title="",
                      created_at="t", updated_at="t")
    store.write_meta(session)
    store.append_message(session.id, "user", "GOAL")

    # 注入持续磁盘错误，确保最终清理不能依赖重试恰好成功
    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "append_message" if failure == "history" else "write_compacted", fail)
    events: list[Any] = []

    # 收集运行终态以检查保存失败是否被错误报告为成功
    async def collect(event: Any) -> None:
        events.append(event)

    runner = AgentRunner(config, provider=CompactingProvider(1), extra_handlers=[collect])
    outcome = await runner.run_and_capture("GOAL", session=session, store=store)
    browser.close.assert_awaited_once()
    assert outcome.status == "failed"
    assert outcome.reason == "persistence_error"
    assert [event.status for event in events if event.type == "run.finished"] == ["failed"]
