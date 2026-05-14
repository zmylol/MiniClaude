from __future__ import annotations

from mini_claude.core.context import ExecutionContext


def test_initial_message_is_goal() -> None:
    ctx = ExecutionContext(run_id="r1", goal="test goal", max_steps=5)
    assert ctx.messages == [{"role": "user", "content": "test goal"}]


def test_is_done_returns_false_when_running() -> None:
    ctx = ExecutionContext(run_id="r1", goal="g", max_steps=5)
    assert not ctx.is_done()


def test_mark_success() -> None:
    ctx = ExecutionContext(run_id="r1", goal="g", max_steps=5)
    ctx.mark_success()
    assert ctx.is_done()
    assert ctx.status == "success"
    assert ctx.reason is None


def test_mark_failed() -> None:
    ctx = ExecutionContext(run_id="r1", goal="g", max_steps=5)
    ctx.mark_failed("exceeded_max_steps")
    assert ctx.is_done()
    assert ctx.status == "failed"
    assert ctx.reason == "exceeded_max_steps"


def test_add_assistant_message_appended() -> None:
    ctx = ExecutionContext(run_id="r1", goal="g", max_steps=5)
    content = [{"type": "text", "text": "I'll help"}]
    ctx.add_assistant_message(content)
    assert ctx.messages[-1] == {"role": "assistant", "content": content}


def test_add_tool_result_creates_user_message() -> None:
    ctx = ExecutionContext(run_id="r1", goal="g", max_steps=5)
    ctx.add_assistant_message(
        [{"type": "tool_use", "id": "toolu_01", "name": "read_file", "input": {"path": "x"}}]
    )
    ctx.add_tool_result("toolu_01", "file content")
    last = ctx.messages[-1]
    assert last["role"] == "user"
    assert last["content"][0]["type"] == "tool_result"
    assert last["content"][0]["tool_use_id"] == "toolu_01"
    assert last["content"][0]["content"] == "file content"


def test_multiple_tool_results_share_one_message() -> None:
    ctx = ExecutionContext(run_id="r1", goal="g", max_steps=5)
    ctx.add_assistant_message([
        {"type": "tool_use", "id": "toolu_01", "name": "read_file", "input": {}},
        {"type": "tool_use", "id": "toolu_02", "name": "read_file", "input": {}},
    ])
    ctx.add_tool_result("toolu_01", "result A")
    ctx.add_tool_result("toolu_02", "result B")

    # goal + assistant + tool_results（合并为一条）
    assert len(ctx.messages) == 3
    last = ctx.messages[-1]
    assert last["role"] == "user"
    assert len(last["content"]) == 2
    assert last["content"][0]["tool_use_id"] == "toolu_01"
    assert last["content"][1]["tool_use_id"] == "toolu_02"


def test_tool_result_error_flag() -> None:
    ctx = ExecutionContext(run_id="r1", goal="g", max_steps=5)
    ctx.add_assistant_message(
        [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]
    )
    ctx.add_tool_result("t1", "something failed", is_error=True)
    block = ctx.messages[-1]["content"][0]
    assert block["is_error"] is True
    assert block["content"] == "something failed"


def test_message_order_across_steps() -> None:
    ctx = ExecutionContext(run_id="r1", goal="g", max_steps=5)
    ctx.add_assistant_message([{"type": "text", "text": "step 1 plan"}])
    ctx.add_tool_result("t1", "tool result")
    ctx.add_assistant_message([{"type": "text", "text": "step 2 plan"}])

    roles = [m["role"] for m in ctx.messages]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_step_counter_default() -> None:
    ctx = ExecutionContext(run_id="r1", goal="g", max_steps=20)
    assert ctx.step == 0
