from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mini_claude.tui.app import ChatTextArea, LLMStreamBlock, MiniTuiApp, PermissionSelect


# 功能：外部会话的文本、完成和关闭不能污染当前 TUI，会话子代理也不能结束主运行
# 设计：真实 Textual DOM 接收交错事件，同时检查内容、busy 及输入状态
async def test_tui_scopes_interleaved_sessions_and_child_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MiniTuiApp, "on_mount", lambda self: None)
    app = MiniTuiApp("127.0.0.1", 9999)
    app._session_id = "session-a"
    async with app.run_test() as pilot:
        app._handle_event({"type": "run.started", "run_id": "a", "session_id": "session-a"})
        app._busy = True
        prompt = app.query_one("#prompt", ChatTextArea)
        prompt.disabled = True
        for event in [
            {"type": "run.started", "run_id": "b", "session_id": "session-b"},
            {"type": "llm.token", "run_id": "b", "session_id": "session-b", "token": "foreign"},
            {"type": "session.waiting_for_input", "session_id": "session-b"},
            {"type": "session.closed", "session_id": "session-b"},
            {"type": "run.started", "run_id": "child", "root_run_id": "a", "parent_run_id": "a", "session_id": "session-a"},
            {"type": "run.finished", "run_id": "child", "root_run_id": "a", "parent_run_id": "a", "session_id": "session-a", "status": "success"},
        ]:
            app._handle_event(event)
        await pilot.pause()
        assert app._busy and prompt.disabled
        assert not list(app.query(LLMStreamBlock))


# 功能：响应完成按运行和步骤替换临时流，空响应、失败与迟到事件不能保留残句或覆盖下一轮
# 设计：插入跨步骤和跨运行回复后回放旧完成事件，断言每个真实显示块的最终文本
async def test_tui_reconciles_complete_empty_and_failed_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MiniTuiApp, "on_mount", lambda self: None)
    app = MiniTuiApp("127.0.0.1", 9999)
    app._session_id = "s"
    async with app.run_test() as pilot:
        for event in [
            {"type": "llm.token", "run_id": "r", "step": 1, "token": "half"},
            {"type": "llm.response.completed", "run_id": "r", "step": 1, "text": "complete"},
            {"type": "llm.token", "run_id": "r", "step": 1, "token": "late"},
            {"type": "llm.token", "run_id": "r", "step": 2, "token": "empty half"},
            {"type": "llm.response.completed", "run_id": "r", "step": 2, "text": ""},
            {"type": "llm.token", "run_id": "r", "step": 3, "token": "failed half"},
            {"type": "llm.response.failed", "run_id": "r", "step": 3, "reason": "cancelled"},
            {"type": "llm.response.completed", "run_id": "next", "step": 1, "text": "next answer"},
            {"type": "llm.response.completed", "run_id": "r", "step": 1, "text": "complete"},
        ]:
            app._handle_event({**event, "session_id": "s"})
        await pilot.pause()
        assert [block._text for block in app.query(LLMStreamBlock)] == ["complete", "", "", "next answer"]


# 功能：其他客户端批准审批会清除对应卡片，RPC 失败保留审批且不放开仍运行中的输入
# 设计：真实选择控件模拟外部重复批准和本地发送失败，覆盖当前提前删除并吞掉错误的路径
async def test_tui_approval_settlement_and_failed_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MiniTuiApp, "on_mount", lambda self: None)
    app = MiniTuiApp("127.0.0.1", 9999)
    app._session_id = "s"
    async with app.run_test() as pilot:
        app._busy = True
        for tool_id in ("external", "local"):
            app._handle_event({"type": "permission.requested", "session_id": "s", "run_id": "r", "tool_use_id": tool_id, "tool_name": "bash"})
            await pilot.pause()
        for _ in range(2):
            app._handle_event({"type": "permission.granted", "session_id": "s", "run_id": "r", "tool_use_id": "external", "decision": "allow_once"})
        await pilot.pause()
        assert list(app._pending_permission_blocks) == [("r", "local")]
        selector = next(iter(app.query(PermissionSelect)))
        app._client = AsyncMock()
        app._client.send_command.side_effect = OSError("send failed")
        await app.on_permission_select_decided(PermissionSelect.Decided(selector, "local", "allow_once"))
        await pilot.pause()
        assert list(app._pending_permission_blocks) == [("r", "local")]
        assert list(app.query(PermissionSelect)) == [selector]
        assert app.query_one("#prompt", ChatTextArea).disabled


# 功能：同会话主代理和子代理的相同工具调用 ID 审批互不覆盖
# 设计：两个真实审批控件共用 ID，仅批准子运行后对主运行执行成功 RPC，检查携带准确 run_id
async def test_tui_permission_ids_are_scoped_by_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MiniTuiApp, "on_mount", lambda self: None)
    app = MiniTuiApp("127.0.0.1", 9999)
    app._session_id = "s"
    async with app.run_test() as pilot:
        app._busy = True
        for run_id in ("main", "child"):
            app._handle_event({"type": "permission.requested", "session_id": "s", "run_id": run_id, "tool_use_id": "same", "tool_name": "bash"})
            await pilot.pause()
        app._handle_event({"type": "permission.granted", "session_id": "s", "run_id": "child", "tool_use_id": "same", "decision": "allow_once"})
        await pilot.pause()
        assert list(app._pending_permission_blocks) == [("main", "same")]
        selector = next(iter(app.query(PermissionSelect)))
        app._client = AsyncMock()
        await app.on_permission_select_decided(PermissionSelect.Decided(selector, "same", "allow_once"))
        app._client.send_command.assert_awaited_once_with(
            "permission.respond", {"run_id": "main", "tool_use_id": "same", "decision": "allow_once"},
        )
        await pilot.pause()
        app._handle_event({"type": "permission.granted", "session_id": "s", "run_id": "main", "tool_use_id": "same", "decision": "allow_once"})
        await pilot.pause()
        assert not list(app.query(PermissionSelect))
        assert app.query_one("#prompt", ChatTextArea).disabled


# 功能：回放只显示请求运行及其旧格式嵌套子运行，不接受同会话其他运行
# 设计：仅根和直接子事件声明父运行，孙运行依靠已恢复映射，外部运行携带相同会话也应拒绝
def test_tui_replay_filters_run_tree_and_keeps_legacy_descendants() -> None:
    app = MiniTuiApp("127.0.0.1", 9999, replay_run_id="root")
    appended = []
    app._append = appended.append
    for event in [
        {"type": "run.started", "run_id": "root", "session_id": "s"},
        {"type": "subagent.started", "run_id": "child", "parent_run_id": "root"},
        {"type": "subagent.started", "run_id": "grandchild", "parent_run_id": "child"},
        {"type": "llm.token", "run_id": "grandchild", "token": "nested"},
        {"type": "llm.token", "run_id": "other", "session_id": "s", "token": "foreign"},
    ]:
        app._handle_event(event)
    assert [block._text for block in appended if isinstance(block, LLMStreamBlock)] == ["nested"]


# 功能：审批 RPC 空成功不能覆盖其他客户端已接受的相反决定
# 设计：本地允许请求先返回但拒绝通知延后到达，卡片应保持待确认直到显示服务端拒绝
async def test_tui_waits_for_authoritative_permission_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MiniTuiApp, "on_mount", lambda self: None)
    app = MiniTuiApp("127.0.0.1", 9999)
    app._session_id = "s"
    async with app.run_test() as pilot:
        app._handle_event({"type": "permission.requested", "session_id": "s", "run_id": "r", "tool_use_id": "t", "tool_name": "bash"})
        await pilot.pause()
        block = app._pending_permission_blocks[("r", "t")]
        selector = next(iter(app.query(PermissionSelect)))
        app._client = AsyncMock()
        app._client.send_command.return_value = {}
        await app.on_permission_select_decided(PermissionSelect.Decided(selector, "t", "allow_once"))
        assert ("r", "t") in app._pending_permission_blocks
        assert not block._resolved
        app._handle_event({"type": "permission.denied", "session_id": "s", "run_id": "r", "tool_use_id": "t", "decision": "deny_once"})
        await pilot.pause()
        assert "denied" in block.content
        assert "allowed" not in block.content
