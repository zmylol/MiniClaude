from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import mini_claude.cli.commands.chat as chat_command
import mini_claude.cli.commands.run as run_command
from mini_claude.cli.commands.chat import ChatPrinter
from mini_claude.cli.commands.run import StdoutPrinter
from mini_claude.core.config import MiniConfig


# 功能：验证 run.started 事件在 stdout 中打印 [run] 前缀和 run_id
# 设计：用 capsys 捕获 stdout，直接断言关键字符串，避免对格式细节过度约束
async def test_run_started_prints_run_id(capsys: pytest.CaptureFixture[str]) -> None:
    printer = StdoutPrinter()
    await printer.handle(
        {"type": "run.started", "run_id": "20260515-abc", "goal": "g", "ts": "t"}
    )
    out = capsys.readouterr().out
    assert "[run]" in out
    assert "20260515-abc" in out


# 功能：验证 step.started 事件打印 [step N] 和 planning... 文本
# 设计：断言步骤编号和 planning 关键词同时出现，覆盖格式模板的两个可变部分
async def test_step_started_prints_step_number(capsys: pytest.CaptureFixture[str]) -> None:
    printer = StdoutPrinter()
    await printer.handle({"type": "step.started", "run_id": "r", "step": 3, "ts": "t"})
    out = capsys.readouterr().out
    assert "[step 3]" in out
    assert "planning" in out


# 功能：旧日志未发送完整响应事件时，在步骤完成后输出缓存 token
# 设计：先检查 stdout 尚未写入，再以旧 step.finished 作为边界兼容回放
async def test_legacy_llm_token_flushes_on_step_finished(
    capsys: pytest.CaptureFixture[str],
) -> None:
    printer = StdoutPrinter()
    await printer.handle({"type": "llm.token", "run_id": "r", "token": "hello", "ts": "t"})
    assert capsys.readouterr().out == ""

    await printer.handle({"type": "step.finished", "run_id": "r", "step": 1, "ts": "t"})
    out = capsys.readouterr().out
    assert "hello" in out
    assert "[step 1]" in out


# 功能：验证 tool.call_started 打印工具名和 JSON 序列化的 params
# 设计：用带 Unicode 内容的 params 检查 ensure_ascii=False（保留中文字符），断言工具名和参数都出现
async def test_tool_call_started_prints_name_and_params(
    capsys: pytest.CaptureFixture[str],
) -> None:
    printer = StdoutPrinter()
    await printer.handle(
        {
            "type": "tool.call_started",
            "run_id": "r",
            "tool_use_id": "t1",
            "tool_name": "read_file",
            "params": {"path": "README.md"},
            "ts": "t",
        }
    )
    out = capsys.readouterr().out
    assert "[tool]" in out
    assert "read_file" in out
    assert "README.md" in out


# 功能：验证 run.finished 打印 status 和 steps 字段
# 设计：success 路径下断言 status 和 steps 出现在输出中，不检查 elapsed 的精确值（依赖时间）
async def test_run_finished_prints_status_and_steps(capsys: pytest.CaptureFixture[str]) -> None:
    printer = StdoutPrinter()
    await printer.handle(
        {"type": "run.started", "run_id": "r", "goal": "g", "ts": "t"}
    )
    await printer.handle(
        {"type": "run.finished", "run_id": "r", "status": "success", "steps": 4, "ts": "t"}
    )
    out = capsys.readouterr().out
    assert "success" in out
    assert "4" in out


@pytest.mark.parametrize("printer_class", [StdoutPrinter, ChatPrinter])
# 功能：CLI 输出仅写入完整权威响应，失败半句和重复完成均不进入正式 stdout
# 设计：同时检查两个消费者，模拟失败重试成功及取消，并在 token 后立刻读取不可回滚的输出
async def test_stdout_buffers_and_reconciles_response(
    capsys: pytest.CaptureFixture[str], printer_class: type[StdoutPrinter] | type[ChatPrinter],
) -> None:
    printer = printer_class()
    await printer.handle({"type": "llm.token", "run_id": "r", "step": 1, "token": "half"})
    assert capsys.readouterr().out == ""
    await printer.handle({"type": "llm.response.completed", "run_id": "r", "step": 1, "text": "complete"})
    await printer.handle({"type": "llm.response.completed", "run_id": "r", "step": 1, "text": "complete"})
    await printer.handle({"type": "llm.token", "run_id": "r", "step": 2, "token": "cancelled half"})
    await printer.handle({"type": "llm.response.failed", "run_id": "r", "step": 2, "reason": "cancelled"})
    await printer.handle({"type": "run.finished", "run_id": "r", "status": "success"})
    output = capsys.readouterr().out
    assert output.count("complete") == 1
    assert "half" not in output


# 功能：CLI run 回包之前的并发事件只输出本次运行，其他运行完成不能提前结束等待
# 设计：IPC 替身先推送外部和自身事件再返回 run_id，真实打印器验证缓冲重放的范围
async def test_run_command_filters_events_before_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    client = MagicMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()

    # 在命令执行期间模拟 IPC 事件循环仍保持连接
    async def event_loop() -> None:
        await asyncio.Event().wait()

    # 用回包前事件流复现共享订阅引起的外部答案和错误混入
    async def send(method: str, params: dict) -> dict:
        if method == "agent.run":
            handler = client.on_event.call_args.args[0]
            for event in [
                {"type": "run.started", "run_id": "foreign"},
                {"type": "llm.response.completed", "run_id": "foreign", "step": 1, "text": "foreign answer"},
                {"type": "run.finished", "run_id": "foreign", "status": "failed"},
                {"type": "run.started", "run_id": "owned"},
                {"type": "llm.token", "run_id": "owned", "step": 1, "token": "half"},
                {"type": "llm.response.completed", "run_id": "owned", "step": 1, "text": "owned answer"},
                {"type": "run.finished", "run_id": "owned", "status": "success"},
            ]:
                await handler(event)
            return {"run_id": "owned"}
        return {}

    client.run_event_loop = event_loop
    client.send_command = send
    monkeypatch.setattr(run_command, "SocketClient", lambda host, port: client)
    assert await run_command._run_async("goal", MiniConfig()) == 0
    output = capsys.readouterr().out
    assert "owned answer" in output and "foreign" not in output and "half" not in output


# 功能：CLI chat 按会话隔离文本，并保留同 ID 的其他运行审批
# 设计：交错主子运行审批与外部回复，批准子运行后主运行审批仍可继续处理
async def test_chat_scopes_pending_permissions_by_run(capsys: pytest.CaptureFixture[str]) -> None:
    printer = ChatPrinter()
    printer.session_id = "s"
    for run_id in ("main", "child"):
        await printer.handle({"type": "permission.requested", "session_id": "s", "run_id": run_id, "tool_use_id": "same"})
    await printer.handle({"type": "permission.granted", "session_id": "s", "run_id": "child", "tool_use_id": "same", "decision": "allow_once"})
    await printer.handle({"type": "llm.response.completed", "session_id": "foreign", "run_id": "foreign", "step": 1, "text": "foreign answer"})
    assert printer.pending_permission_id == "same"
    assert printer.pending_permission_run == "main"
    assert "foreign" not in capsys.readouterr().out


# 功能：chat 审批空成功后保留待确认状态，延迟拒绝通知才清理并显示最终决定
# 设计：真实输入循环读取一次允许后检查打印器状态，再注入其他客户端已接受的拒绝结果
async def test_chat_waits_for_authoritative_permission_decision(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    client = MagicMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.send_command = AsyncMock(return_value={"session_id": "s"})
    printer = ChatPrinter()
    inputs = 0

    # 模拟 IPC 事件循环，测试结束时由命令入口正常取消
    async def event_loop() -> None:
        await asyncio.Event().wait()

    # 对方决定事件比本地空成功晚到，输入期间检查不可提前清理审批
    async def readline(prompt: str) -> str:
        nonlocal inputs
        inputs += 1
        if inputs == 1:
            await printer.handle({"type": "permission.requested", "session_id": "s", "run_id": "r", "tool_use_id": "t", "tool_name": "bash"})
            return "y"
        assert printer.pending_permission_id == "t"
        await printer.handle({"type": "permission.denied", "session_id": "s", "run_id": "r", "tool_use_id": "t", "decision": "deny_once"})
        raise EOFError

    client.run_event_loop = event_loop
    monkeypatch.setattr(chat_command, "SocketClient", lambda host, port: client)
    monkeypatch.setattr(chat_command, "ChatPrinter", lambda: printer)
    monkeypatch.setattr(chat_command, "_readline", readline)
    assert await chat_command._chat_async(MiniConfig()) == 0
    assert printer.pending_permission_id is None
    assert "deny_once" in capsys.readouterr().out
