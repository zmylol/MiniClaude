from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from mini_claude.core.app import CoreApp
from mini_claude.core.bus.envelope import HandlerError
from mini_claude.core.desktop_services import ManagedMcpServers
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.base import LLMProvider
from mini_claude.core.llm.types import LlmResponse
from mini_claude.core.runner import RunOutcome
from mini_claude.core.session.manager import SessionManager
from mini_claude.core.session.store import SessionStore


# 功能：创建一次性会话期间开始插件变更时，不得随后启动使用旧连接的运行。
# 设计：阻塞真实会话创建事件，验证插件互斥在异步边界后重新检查且没有调用 runner。
async def test_one_shot_rechecks_plugins_after_session_creation(tmp_path: Path) -> None:
    app = CoreApp()
    entered, release = asyncio.Event(), asyncio.Event()
    runner = MagicMock()
    runner.run_and_capture = AsyncMock(
        return_value=RunOutcome(status="success", result="unused", reason=None),
    )

    # 功能：在会话已创建但一次性运行尚未登记时暂停请求。
    # 设计：用事件总线的真实可等待订阅者暴露并发窗口，不替换待测处理器。
    async def pause_created(event: BaseModel) -> None:
        if event.type == "session.created":
            entered.set()
            await release.wait()

    app._bus.subscribe(pause_created)
    sessions = SessionManager(
        SessionStore(tmp_path / "sessions"), lambda: runner, app._bus, project_path=tmp_path,
    )
    plugins = ManagedMcpServers(
        tmp_path, is_busy=lambda: bool(app._running_runs) or sessions.has_active_runs(),
    )
    app._sessions, app._mcp_manager = sessions, plugins
    request = asyncio.create_task(app._agent_run_handler({"goal": "review project"}))
    await asyncio.wait_for(entered.wait(), 3)
    plugins._begin_change()
    release.set()
    with pytest.raises(HandlerError, match="插件正在更新"):
        await asyncio.wait_for(request, 3)
    assert not app._running_runs
    runner.run_and_capture.assert_not_called()


# 功能：手动压缩遵循会话当前模型，同时保留旧调用方直接注入 provider 的能力。
# 设计：真实压缩器生成持久摘要，只替换外部模型；两轮切换确认模型不是创建时快照。
@pytest.mark.parametrize("use_factory", [True, False])
async def test_manual_compaction_uses_current_session_model(
    tmp_path: Path, use_factory: bool,
) -> None:
    selected: list[str] = []
    fallback = MagicMock(spec=LLMProvider)
    fallback.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="legacy"))

    # 功能：按所选模型创建只返回摘要的 provider。
    # 设计：记录工厂输入并让摘要携带模型标识，验证选择确实用于持久化的压缩结果。
    def provider_factory(model: str) -> LLMProvider:
        selected.append(model)
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text=model))
        return provider

    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store, MagicMock(), EventBus(), provider=fallback,
        provider_factory=provider_factory if use_factory else None,
        project_path=tmp_path, default_model="default-model",
    )
    session = await manager.create("chat", model="first-model")
    for model in ("first-model", "second-model"):
        await manager.configure(session.id, model=model)
        store.append_message(session.id, "user", "conversation details " * 30)
        await manager.compact(session.id)
        context = store.read_messages(session.id)
        assert context[0]["content"] == (model if use_factory else "legacy")
        assert len(context) == 2
        assert all(message["content"] == "conversation details " * 30
                   for message in await manager.get_history(session.id))
    assert selected == (["first-model", "second-model"] if use_factory else [])
    assert fallback.chat.await_count == (0 if use_factory else 2)
