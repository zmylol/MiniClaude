from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console
from textual.widgets import Static

from mini_claude.core.bus.events import LlmResponseCompletedEvent
from mini_claude.tui.app import LLMStreamBlock, MiniTuiApp, ToolCallBlock


# 功能：完成事件往返保留有序内容和停止原因，兼容旧的纯文本事件。
# 设计：使用真实 Pydantic 序列化而非字典替身，防止新增字段在协议边界被忽略。
def test_completed_event_preserves_content_and_legacy_defaults() -> None:
    content = [{"type": "server_tool_use", "id": "search", "name": "web_search", "input": {"query": "docs"}}]
    event = LlmResponseCompletedEvent.model_validate({
        "run_id": "r", "step": 1, "text": "answer", "ts": "t",
        "content": content, "stop_reason": "pause_turn",
    })
    restored = LlmResponseCompletedEvent.model_validate_json(event.model_dump_json()).model_dump()
    assert restored.get("content") == content
    assert restored.get("stop_reason") == "pause_turn"
    legacy = LlmResponseCompletedEvent(run_id="r", step=1, text="legacy", ts="t").model_dump()
    assert legacy.get("content") == []
    assert legacy.get("stop_reason") == "end_turn"


# 功能：TUI 实时与事件历史重放展示搜索查询和配对结果，失败可见且重复事件不重复渲染。
# 设计：真实 Textual DOM 混合服务端搜索和本地工具，渲染终端文本检查顺序与不透明字段隔离。
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("replay", [False, True])
async def test_tui_renders_search_content_and_replays_once(
    monkeypatch: pytest.MonkeyPatch, failed: bool, replay: bool,
) -> None:
    monkeypatch.setattr(MiniTuiApp, "on_mount", lambda self: None)
    app = MiniTuiApp("127.0.0.1", 9999, replay_run_id="r" if replay else None)
    if not replay:
        app._session_id = "s"
    result = (
        {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}
        if failed else [{"type": "web_search_result", "title": "Docs [bold]literal[/bold]", "url": "https://example.com/docs", "encrypted_content": "SECRET-RESULT"}]
    )
    event = {
        "type": "llm.response.completed", "session_id": "s", "run_id": "r", "step": 1,
        "text": "BeforeAfter", "stop_reason": "tool_use", "content": [
            {"type": "thinking", "thinking": "internal", "signature": "SECRET-SIGNATURE"},
            {"type": "redacted_thinking", "data": "SECRET-REDACTED"},
            {"type": "text", "text": "Before"},
            {"type": "server_tool_use", "id": "search", "name": "web_search_prime", "input": {"query": "Claude API"}},
            {"type": "web_search_tool_result", "tool_use_id": "search", "content": result},
            {"type": "text", "text": "After"},
            {"type": "tool_use", "id": "local", "name": "read_file", "input": {"path": "a.py"}},
        ],
    }
    async with app.run_test() as pilot:
        app._handle_event({"type": "llm.token", "session_id": "s", "run_id": "r", "step": 1, "token": "partial"})
        app._handle_event(event)
        app._handle_event({"type": "tool.call_started", "session_id": "s", "run_id": "r", "tool_use_id": "local", "tool_name": "read_file", "params": {"path": "a.py"}})
        app._handle_event(event)
        await pilot.pause()
        assert len(app.query(LLMStreamBlock)) == 1
        assert len(app.query(ToolCallBlock)) == 1
        output = StringIO()
        Console(file=output, force_terminal=False, width=120).print(app.query_one(LLMStreamBlock).content)
        rendered = output.getvalue()
        assert "Claude API" in rendered
        assert rendered.index("Before") < rendered.index("Claude API") < rendered.index("After")
        assert "SECRET" not in rendered
        assert "partial" not in rendered
        if failed:
            assert "失败" in rendered and "max_uses_exceeded" in rendered
        else:
            assert "Docs [bold]literal[/bold]" in rendered
            assert "https://example.com/docs" in rendered


# 功能：TUI 将模型截断和拒绝原因显示为中文，同时保留正式响应文本。
# 设计：直接投递停止事件检查错误控件内容，覆盖 UI 映射而不依赖真实模型。
@pytest.mark.parametrize(("reason", "label"), [
    ("max_tokens", "输出达到上限"),
    ("model_context_window_exceeded", "上下文已满"),
    ("refusal", "模型拒绝"),
    ("unexpected_stop_reason", "响应不完整"),
    ("incomplete_response", "响应不完整"),
    ("server_tool_unavailable", "服务器搜索不可用"),
])
def test_tui_explains_response_stop_reason(reason: str, label: str) -> None:
    app = MiniTuiApp("127.0.0.1", 9999)
    app._session_id = "s"
    appended: list[Static] = []
    app._append = appended.append  # type: ignore[method-assign, assignment]
    app._handle_event({"type": "run.finished", "session_id": "s", "run_id": "r", "status": "failed", "reason": reason})
    assert label in str(appended[-1].content)


# 功能：续写返回搜索结果时更新旧步骤卡片，运行之间复用调用 ID 仍保持独立。
# 设计：真实 Textual DOM 接收两个交错运行和迟到结果，再重放旧完成事件检查幂等。
async def test_tui_pairs_search_results_across_steps_without_crossing_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MiniTuiApp, "on_mount", lambda self: None)
    app = MiniTuiApp("127.0.0.1", 9999)
    app._session_id = "s"
    starts = [{
        "type": "llm.response.completed", "session_id": "s", "run_id": run,
        "step": 1, "text": f"Before {run}", "stop_reason": "pause_turn", "content": [
            {"type": "text", "text": f"Before {run}"},
            {"type": "server_tool_use", "id": "shared", "name": "web_search", "input": {"query": f"{run} query"}},
        ],
    } for run in ("a", "b")]
    result = {
        "type": "llm.response.completed", "session_id": "s", "run_id": "a", "step": 2,
        "text": "After a", "content": [
            {"type": "web_search_tool_result", "tool_use_id": "shared", "content": [{"type": "web_search_result", "title": "A result", "url": "https://example.com/a"}]},
            {"type": "text", "text": "After a"},
        ],
    }
    async with app.run_test() as pilot:
        for event in starts:
            app._handle_event(event)
        app._handle_event(result)
        await pilot.pause()
        blocks = list(app.query(LLMStreamBlock))
        rendered = []
        for block in blocks:
            output = StringIO()
            Console(file=output, force_terminal=False, width=120).print(block.content)
            rendered.append(output.getvalue())
        assert "A result" in rendered[0] and "已完成" in rendered[0]
        assert "等待结果" in rendered[1] and "A result" not in rendered[1]
        app._handle_event({**result, "run_id": "b", "text": "", "content": [
            {"type": "web_search_tool_result", "tool_use_id": "shared", "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"}},
        ]})
        app._handle_event(result)
        app._handle_event(starts[0])
        await pilot.pause()
        output = StringIO()
        Console(file=output, force_terminal=False, width=120).print(blocks[1].content)
        assert "失败" in output.getvalue() and "unavailable" in output.getvalue()
        assert len(app.query(LLMStreamBlock)) == 4
        assert not list(app.query(ToolCallBlock))
