from __future__ import annotations

from typing import Any

from mini_claude.tui.app import MiniTuiApp


class _FakeLog:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)


# 功能：验证 llm.token 事件被累积进 token 缓冲区，下一个非 token 事件触发整体写入
# 设计：用 _FakeLog 代替真实 RichLog，绕开 Textual 渲染层，直接测试缓冲逻辑；
#       断言缓冲区在 token 事件后非空，在 step 事件后清空并整体写入，验证一行性
def test_llm_tokens_buffered_and_flushed_together() -> None:
    app = MiniTuiApp("127.0.0.1", 9999)
    log: Any = _FakeLog()

    app._handle_event({"type": "llm.token", "token": "Hello", "run_id": "r", "ts": "t"}, log)
    app._handle_event({"type": "llm.token", "token": " world", "run_id": "r", "ts": "t"}, log)
    assert app._token_buf == "Hello world"  # type: ignore[attr-defined]
    assert log.lines == []  # not yet written

    app._handle_event(
        {"type": "step.finished", "run_id": "r", "step": 1, "ts": "t"}, log
    )
    assert app._token_buf == ""  # type: ignore[attr-defined]
    assert any("Hello world" in line for line in log.lines)


# 功能：验证 run.started 事件将 run_id 和 goal 写入日志
# 设计：_FakeLog 记录所有 write 调用，断言 run_id 和 goal 出现在某行，
#       不约束格式细节，避免日志字符串变化导致测试脆化
def test_run_started_writes_run_id_and_goal() -> None:
    app = MiniTuiApp("127.0.0.1", 9999)
    log: Any = _FakeLog()

    app._handle_event(
        {"type": "run.started", "run_id": "20260515-abc", "goal": "test the thing", "ts": "t"},
        log,
    )
    combined = "\n".join(log.lines)
    assert "20260515-abc" in combined
    assert "test the thing" in combined


# 功能：验证 run.finished 按 status 决定颜色标记（success=green，failed=red）
# 设计：分别发送 success 和 failed 事件，检查输出中对应颜色标签存在，
#       两次使用同一 app 实例以确认状态不被前次调用污染
def test_run_finished_color_depends_on_status() -> None:
    app = MiniTuiApp("127.0.0.1", 9999)
    log_success: Any = _FakeLog()
    log_failed: Any = _FakeLog()

    app._handle_event(
        {"type": "run.finished", "run_id": "r", "status": "success", "steps": 2, "ts": "t"},
        log_success,
    )
    app._handle_event(
        {"type": "run.finished", "run_id": "r", "status": "failed", "steps": 3, "ts": "t"},
        log_failed,
    )

    assert any("green" in line for line in log_success.lines)
    assert any("red" in line for line in log_failed.lines)


# 功能：验证 unknown type 的事件不写入日志也不抛异常（静默忽略）
# 设计：发送未知 type，断言 write 未被调用，确认 _handle_event 的默认路径是静默忽略而非报错
def test_unknown_event_type_is_silently_ignored() -> None:
    app = MiniTuiApp("127.0.0.1", 9999)
    log: Any = _FakeLog()

    app._handle_event({"type": "some.unknown.event", "run_id": "r", "ts": "t"}, log)
    assert log.lines == []
