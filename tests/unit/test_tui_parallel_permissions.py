from __future__ import annotations

import pytest

from mini_claude.tui.app import ChatTextArea, MiniTuiApp, PermissionSelect


# 功能：并行审批中第二个工具超时，只移除对应选择器并保留第一个审批。
# 设计：运行真实 Textual DOM，避免替身隐藏 query_one 总是命中第一个控件的问题。
async def test_denied_permission_removes_only_matching_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MiniTuiApp, "on_mount", lambda self: None)
    app = MiniTuiApp("127.0.0.1", 9999)

    async with app.run_test() as pilot:
        for tool_use_id in ("first", "second"):
            app._handle_event({
                "type": "permission.requested",
                "tool_use_id": tool_use_id,
                "tool_name": "bash",
                "param_preview": tool_use_id,
                "run_id": "run-1",
                "ts": "t",
            })
            await pilot.pause()

        selectors = list(app.query(PermissionSelect))
        assert [select._tool_use_id for select in selectors] == ["first", "second"]
        prompt = app.query_one("#prompt", ChatTextArea)
        assert prompt.disabled

        app._handle_event({
            "type": "permission.denied",
            "tool_use_id": "second",
            "decision": "timeout",
            "run_id": "run-1",
            "ts": "t",
        })
        await pilot.pause()

        assert list(app.query(PermissionSelect)) == [selectors[0]]
        assert set(app._pending_permission_blocks) == {"first"}
        assert prompt.disabled
