from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.types import LlmResponse, ToolCallBlock
from mini_claude.core.skills.loader import Skill
from tests.unit.test_history_regressions import manager_for


class BackgroundProvider:
    # 准备可控的子任务和跨轮结果查询
    def __init__(self, fail: bool = False) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.child_id = ""
        self.fail = fail
        self.query = ""

    # 子任务等待信号，第二轮通过真实 agent_result 工具查询第一轮结果
    async def chat(self, *, messages: Any, run_id: str, step: int, **kwargs: Any) -> LlmResponse:
        if messages[0]["content"] == "CHILD":
            self.child_id = run_id
            self.started.set()
            try:
                await self.release.wait()
                if self.fail:
                    raise RuntimeError("child model failed")
                return LlmResponse(stop_reason="end_turn", text="CHILD RESULT")
            finally:
                self.finished.set()
        if step == 1:
            if run_id == "first":
                return LlmResponse(stop_reason="tool_use", tool_calls=[ToolCallBlock(
                    id="spawn", name="spawn_agent", input={"description": "child",
                    "prompt": "CHILD", "run_in_background": True},
                )])
            return LlmResponse(stop_reason="tool_use", tool_calls=[ToolCallBlock(
                id="query", name="agent_result", input={"run_id": self.child_id},
            )])
        if run_id != "first":
            self.query = json.dumps(messages[-1])
        return LlmResponse(stop_reason="end_turn", text="ROOT DONE")


# 功能：同会话第二轮可查询第一轮后台任务，成功和失败均保留真实状态
# 设计：真实父子 AgentLoop 和注册表，使用信号控制后台任务完成时间
@pytest.mark.parametrize("fail", [False, True])
async def test_background_result_survives_turns(tmp_path: Path, fail: bool) -> None:
    provider = BackgroundProvider(fail)
    manager = manager_for(tmp_path, provider)
    session = await manager.create("chat")
    try:
        await manager.send_message(session.id, "SPAWN", run_id="first")
        await asyncio.wait_for(provider.started.wait(), 2)
        await manager.send_message(session.id, "QUERY", run_id="second")
        assert "still running" in provider.query
        provider.release.set()
        await asyncio.wait_for(provider.finished.wait(), 2)
        await asyncio.sleep(0)
        await manager.send_message(session.id, "QUERY", run_id="third")
        assert "Unknown run_id" not in provider.query
        if fail:
            assert '"is_error": true' in provider.query
            assert "llm_error" in provider.query
        else:
            assert "CHILD RESULT" in provider.query
    finally:
        await manager.stop_all()


# 功能：关闭 A 会取消并等待其后台任务，B 的后台运行不受影响
# 设计：两个真实会话共享事件总线，用各自子任务的完成信号验证取消范围
async def test_close_cancels_only_own_background_tasks(tmp_path: Path) -> None:
    bus = EventBus()
    first_provider = BackgroundProvider()
    second_provider = BackgroundProvider()
    first = manager_for(tmp_path / "a", first_provider, bus)
    second = manager_for(tmp_path / "b", second_provider, bus)
    a = await first.create("chat")
    b = await second.create("chat")
    try:
        await first.send_message(a.id, "SPAWN", run_id="first")
        await second.send_message(b.id, "SPAWN", run_id="first")
        await asyncio.wait_for(first_provider.started.wait(), 2)
        await asyncio.wait_for(second_provider.started.wait(), 2)
        await first.close(a.id)
        assert first_provider.finished.is_set()
        assert not first.is_running(a.id)
        assert not second_provider.finished.is_set()
        assert second.is_running(b.id)
    finally:
        await first.stop_all()
        await second.stop_all()


# 功能：Skill 渲染正文进入实际模型输入，同时保留原命令、附件和工具白名单
# 设计：只替换 loader 返回值，检查真实 manager→runner 传给 provider 的参数
async def test_skill_render_reaches_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Provider:
        # 捕获真实模型请求而不调用外部服务
        async def chat(self, **kwargs: Any) -> LlmResponse:
            captured.update(kwargs)
            return LlmResponse(stop_reason="end_turn", text="DONE")

    manager = manager_for(tmp_path, Provider())
    monkeypatch.setattr(manager._skill_loader, "resolve", lambda name: Skill(
        name="target", description="test", system_prompt_template="Read /project/$ARGUMENTS/package.json",
        allowed_tools=["read_file"],
    ))
    session = await manager.create("chat")
    await manager.send_message(session.id, "/target frontend", attachments=[{
        "media_type": "image/png", "data": "test-image",
    }])
    assert "/project/frontend/package.json" in captured["system"]
    assert "$ARGUMENTS" not in captured["system"]
    assert [tool["name"] for tool in captured["tool_schemas"]] == ["read_file"]
    content = (await manager.get_history(session.id))[0]["content"]
    assert content[0]["text"] == "/target frontend"
    assert content[1]["source"]["data"] == "test-image"
