from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from mini_claude.core.llm.types import LlmResponse, ToolCallBlock
from mini_claude.core.subagent.tool import SpawnAgentTool
from mini_claude.core.transport.socket_server import HandlerError
from tests.unit.test_history_regressions import manager_for


@pytest.fixture(autouse=True)
# 隔离单独运行时的上下文和工作目录，子代理只处理本测试提供的固定任务
def isolated_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mini_claude.core.runner.load_context_file", lambda path: "")


class NestedProvider:
    # 为子代理与孙代理分别提供开始、清理信号，精确控制关闭中的并发窗口
    def __init__(self) -> None:
        self.started = {name: asyncio.Event() for name in ("CHILD", "GRAND")}
        self.cleaning = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.cleaned: set[str] = set()
        self.ids: dict[str, str] = {}
        self.query: dict[str, Any] = {}

    # 派生两层后台任务，并让取消清理停在可观察位置以测试阻止新任务的边界
    async def chat(self, *, messages: Any, run_id: str, step: int, **kwargs: Any) -> LlmResponse:
        if run_id == "followup":
            if step == 1:
                return LlmResponse(stop_reason="tool_use", tool_calls=[ToolCallBlock(
                    id="query", name="agent_result", input={"run_id": self.ids["GRAND"]},
                )])
            self.query = messages[-1]["content"][0]
            return LlmResponse(stop_reason="end_turn", text="RESUMED")
        goal = messages[0]["content"]
        if goal in ("ROOT", "CHILD") and step == 1:
            child = "CHILD" if goal == "ROOT" else "GRAND"
            return LlmResponse(stop_reason="tool_use", tool_calls=[ToolCallBlock(
                id="spawn", name="spawn_agent",
                input={"description": child, "prompt": child, "run_in_background": True},
            )])
        if goal == "ROOT":
            return LlmResponse(stop_reason="end_turn", text="ROOT FINISHED")
        self.ids[goal] = run_id
        self.started[goal].set()
        try:
            await asyncio.Future()
        finally:
            self.cleaning.set()
            await self.release_cleanup.wait()
            self.cleaned.add(goal)
        raise AssertionError("unreachable")


# 功能：停止、关闭或删除会话会等待两层后台任务清理，清理中不允许新一轮或新增子代理
# 设计：通过真实派生工具建立主→子→孙运行，在取消 finally 中暂停，验收注册表状态与后续查询
@pytest.mark.parametrize("action", ["cancel", "close", "delete"])
async def test_nested_background_cleanup_blocks_new_work_and_keeps_correct_results(
    tmp_path: Path, action: str,
) -> None:
    provider = NestedProvider()
    manager = manager_for(tmp_path, provider)
    session = await manager.create("chat")
    closing: asyncio.Task[Any] | None = None
    try:
        async with asyncio.timeout(5):
            await manager.send_message(session.id, "ROOT", run_id="root")
            await asyncio.gather(*(signal.wait() for signal in provider.started.values()))
            registry = manager._background[session.id]
            assert registry.is_running()
            closing = asyncio.create_task(getattr(manager, action)(session.id))
            await provider.cleaning.wait()
            assert not closing.done()
            assert not registry.accepting
            with pytest.raises(HandlerError, match="session busy"):
                await manager.send_message(session.id, "SHOULD NOT RUN", run_id="blocked")
            spawn = SpawnAgentTool(
                provider, manager._bus, "root", None, 5, registry,
                manager._store.runs_dir(session.id), session.id,
            )
            result = await spawn.invoke({
                "description": "blocked", "prompt": "UNEXPECTED", "run_in_background": True,
            })
            assert result.is_error
            assert "stopping" in result.content.lower()
            provider.release_cleanup.set()
            await closing
            assert provider.cleaned == {"CHILD", "GRAND"}
            assert not registry.is_running()
            assert registry.all() == []
            if action == "cancel":
                assert registry.accepting
                for run_id in provider.ids.values():
                    outcome = registry.result(run_id)
                    assert outcome is not None and outcome.status == "cancelled"
                await manager.send_message(session.id, "QUERY CANCELLED", run_id="followup")
                assert provider.query["is_error"] is True
                assert "cancelled" in provider.query["content"]
                assert manager._get_session(session.id).status == "waiting_for_input"
            else:
                assert not registry.accepting
                assert all(registry.result(run_id) is None for run_id in provider.ids.values())
                assert session.id not in manager._background
                if action == "delete":
                    assert not manager._store.session_dir(session.id).exists()
                else:
                    assert manager._get_session(session.id).status == "closed"
    finally:
        provider.release_cleanup.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await manager.stop_all()
