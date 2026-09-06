from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mini_claude.core.app import CoreApp
from mini_claude.core.events.bus import EventBus
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.session.manager import SessionManager
from mini_claude.core.session.store import SessionStore


# 功能：重连快照只恢复本会话尚待处理的审批，并在答复后立即移除
# 设计：使用真实 Future、会话目录和 Core 摘要，模拟断线期间仍挂起的工具而不调用模型
async def test_session_summary_restores_only_pending_approvals(tmp_path: Path) -> None:
    permission = PermissionManager(timeout_s=0)
    sessions = SessionManager(
        SessionStore(tmp_path / "sessions"), lambda: None, EventBus(),  # type: ignore[arg-type,return-value]
        permission_manager=permission,
    )
    session = await sessions.create("chat")
    other = await sessions.create("chat")
    app = CoreApp()
    app._sessions = sessions
    app._permission_manager = permission
    emitted = asyncio.Event()

    # 收到审批事件时通知测试读取快照，确保 Future 已登记。
    async def emit(event: dict[str, Any]) -> None:
        emitted.set()

    task = asyncio.create_task(permission.check_and_wait(
        "tool-restore", "write_file", {"path": "hello.txt", "content": "hello"},
        session.id, emit, run_id="run-restore",
    ))
    await emitted.wait()
    result = await app._session_list_handler({})
    summaries = {item.session_id: item for item in result.sessions}
    pending = summaries[session.id].pending_permissions
    assert len(pending) == 1
    assert pending[0].tool_use_id == "tool-restore"
    assert pending[0].run_id == "run-restore"
    assert pending[0].params["path"] == "hello.txt"
    assert summaries[other.id].pending_permissions == []
    pending[0].params["path"] = "mutated"
    assert permission.pending_for(session.id)[0].params["path"] == "hello.txt"
    permission.respond("tool-restore", "allow_once")
    assert permission.pending_for(session.id) == []
    assert await task == (True, "allow_once")
