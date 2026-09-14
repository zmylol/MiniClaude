from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from mini_claude.core.bus.events import RunFinishedEvent, RunStartedEvent
from mini_claude.core.compact.compactor import Compactor
from mini_claude.core.config import MiniConfig
from mini_claude.core.context import ExecutionContext
from mini_claude.core.events.bus import EventBus, EventHandler
from mini_claude.core.events.writer import EventWriter
from mini_claude.core.llm.base import LLMProvider
from mini_claude.core.llm.provider import AnthropicProvider
from mini_claude.core.loop import AgentLoop
from mini_claude.core.mcp.server import McpServerManager
from mini_claude.core.memory.loader import load_context_file
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.runs import RUNS_DIR, new_run_id
from mini_claude.core.session.model import Session
from mini_claude.core.session.store import SessionStore
from mini_claude.core.subagent.registry import BackgroundTaskRegistry
from mini_claude.core.subagent.tool import AgentResultTool, SpawnAgentTool
from mini_claude.core.task.manager import TaskManager
from mini_claude.core.tools.browser import BrowserSession
from mini_claude.core.tools.builtin import (
    BashTool,
    ListDirTool,
    NoteSaveTool,
    ReadFileTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WriteFileTool,
)
from mini_claude.core.tools.builtin.web_fetch import WebFetchTool
from mini_claude.core.tools.builtin.web_search import WebSearchTool
from mini_claude.core.tools.registry import ToolRegistry
from mini_claude.core.trace.provider import TracingProvider
from mini_claude.core.trace.writer import TraceWriter


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None


class AgentRunner:
    # 组装所有运行时依赖，准备执行一次完整的 agent run
    def __init__(
        self,
        config: MiniConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        runs_dir: Path | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
        mcp_manager: McpServerManager | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR
        self._trace = trace
        self._permission_manager = permission_manager
        self._mcp_manager = mcp_manager
        # 跨 run 共享的后台 subagent 任务注册表
        self._task_registry = BackgroundTaskRegistry()

    # 构建工具注册表，注入 TaskManager（任务工具共享同一实例）；可选注入 SpawnAgentTool
    def _build_registry(
        self,
        task_manager: TaskManager,
        *,
        session: Session | None = None,
        store: SessionStore | None = None,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        child_runs_dir: Path | None = None,
        session_id: str = "",
        tool_whitelist: list[str] | None = None,
        browser: BrowserSession | None = None,
    ) -> ToolRegistry:
        allowed: set[str] | None = set(tool_whitelist) if tool_whitelist else None

        def _ok(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        for t in [ReadFileTool(), BashTool(), WriteFileTool(), ListDirTool()]:
            if _ok(t.name):
                registry.register(t)
        if self._config.network.enabled:
            for t in [WebSearchTool(), WebFetchTool()]:
                if _ok(t.name):
                    registry.register(t)
            if browser is not None:
                for browser_tool in browser.get_tools():
                    if _ok(browser_tool.name):
                        registry.register(browser_tool)
        for t in [
            TaskCreateTool(task_manager),
            TaskUpdateTool(task_manager),
            TaskListTool(task_manager),
            TaskGetTool(task_manager),
        ]:
            if _ok(t.name):
                registry.register(t)
        if session is not None and store is not None and run_id is not None:
            note_tool = NoteSaveTool(store, session.id, run_id)
            if _ok(note_tool.name):
                registry.register(note_tool)
        if provider is not None and bus is not None and run_id is not None:
            runs_dir = child_runs_dir or self._runs_dir
            if _ok("spawn_agent"):
                registry.register(
                    SpawnAgentTool(
                        provider=provider,
                        parent_bus=bus,
                        parent_run_id=run_id,
                        permission_manager=self._permission_manager,
                        max_steps=self._config.agent.max_steps,
                        task_registry=self._task_registry,
                        runs_dir=runs_dir,
                        session_id=session_id,
                        depth=0,
                        network_config=self._config.network,
                    )
                )
            if _ok("agent_result"):
                registry.register(AgentResultTool(self._task_registry))
        if self._mcp_manager is not None:
            for mcp_tool in self._mcp_manager.get_tools():
                if _ok(mcp_tool.name):
                    registry.register(mcp_tool)
        return registry

    # 执行一次完整的 agent run（委托给 run_and_capture，忽略返回值）
    async def run(self, goal: str, *, run_id: str | None = None) -> None:
        await self.run_and_capture(goal, run_id=run_id)

    # 检查此运行派生的后台子代理是否仍在执行
    def has_background_runs(self) -> bool:
        return self._task_registry.is_running()

    # 将本轮工具绑定到会话共享的后台任务注册表
    def set_task_registry(self, registry: BackgroundTaskRegistry) -> None:
        self._task_registry = registry

    # 取消并等待所有后台子代理，确保其工具和事件写入器完成清理
    async def cancel_background(self) -> bool:
        return await self._task_registry.cancel()

    # 执行 agent run 并返回 RunOutcome（含最终文字结果）
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        if session is not None and session.model:
            self._config = replace(
                self._config, llm=replace(self._config.llm, default_model=session.model),
            )
        if session is not None and self._permission_manager is not None:
            self._permission_manager.set_session_mode(session.id, session.permission_mode)
        run_id = run_id or new_run_id()
        if session is not None and store is not None:
            run_path = store.runs_dir(session.id) / run_id
            history = store.read_messages(session.id)
            notes = store.read_notes(session.id)
        else:
            run_path = self._runs_dir / run_id
            history = [{"role": "user", "content": goal}]
            notes = ""
        run_path.mkdir(parents=True, exist_ok=True)

        global_ctx = load_context_file(Path("~/.mini/context.md").expanduser())
        project_ctx = load_context_file(Path(".mini/context.md"))

        task_manager = TaskManager(run_path / ".tasks")

        bus = self._bus if self._bus is not None else EventBus()
        bus.register_run(run_id, session.id if session else None)
        for h in self._extra_handlers:
            bus.subscribe(h)

        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
            prefill_messages=history,
            session_notes=notes,
            global_context=global_ctx,
            project_context=project_ctx,
            system_prompt_override=system_prompt_override,
        )
        if session is not None and store is not None:
            context.history_position = store.history_position(session.id)
            committed = 0

            # 每次只追加尚未提交的原始消息，成功后再发布摘要覆盖位置
            def persist(current: ExecutionContext) -> None:
                nonlocal committed
                assert session is not None and store is not None
                while committed < len(current.new_messages):
                    message = current.new_messages[committed]
                    store.append_message(
                        session.id, message["role"], message["content"], run_id=run_id,
                    )
                    committed += 1
                if current.summary_messages is not None:
                    store.write_compacted(
                        session.id, current.summary_messages, current.summary_position,
                    )
                    current.summary_messages = None

            context.persist = persist

        async with EventWriter(run_path / "events.jsonl", run_id=run_id) as writer:
            writer.subscribe(bus)
            await bus.publish(
                RunStartedEvent(
                    run_id=run_id,
                    goal=goal,
                    ts=_now(),
                    session_id=session.id if session is not None else None,
                )
            )

            cancelled = False
            browser = (
                BrowserSession(
                    headless=self._config.network.browser_headless,
                    executable_path=self._config.network.browser_executable_path,
                )
                if self._config.network.enabled and self._config.network.browser_enabled
                else None
            )
            try:
                provider: LLMProvider = self._provider or AnthropicProvider(
                    self._config.llm.default_model
                )
                if self._trace is not None:
                    provider = TracingProvider(
                        provider,
                        self._trace,
                        include_payload=self._config.trace.include_llm_payload,
                    )
                session_id_str = session.id if session is not None else ""
                child_runs_dir = (
                    store.runs_dir(session.id)
                    if session is not None and store is not None
                    else self._runs_dir
                )
                registry = self._build_registry(
                    task_manager,
                    session=session,
                    store=store,
                    run_id=run_id,
                    provider=provider,
                    bus=bus,
                    child_runs_dir=child_runs_dir,
                    session_id=session_id_str,
                    tool_whitelist=tool_whitelist,
                    browser=browser,
                )
                session_dir = (
                    store.session_dir(session.id)
                    if session is not None and store is not None
                    else run_path
                )
                compactor = Compactor(bus, session_dir, session_id_str)
                loop = AgentLoop(
                    provider, registry, bus,
                    permission_manager=self._permission_manager,
                    compactor=compactor,
                    compact_threshold=self._config.compaction.auto_threshold,
                    session_id=session_id_str,
                )
                await loop.run(context)
            except asyncio.CancelledError:
                cancelled = True
                await self.cancel_background()
                if self._permission_manager is not None and session is not None:
                    self._permission_manager.cancel_session(session.id, reason="user_cancelled")
                if not context.is_done():
                    context.mark_failed("cancelled")
            except Exception as exc:
                logging.getLogger(__name__).exception(
                    "agent run failed run_id=%s step=%d", run_id, context.step
                )
                context.mark_failed(
                    "persistence_error" if isinstance(exc, OSError) else "llm_error",
                )
            finally:
                pending: set[str] = set()
                for message in context.new_messages:
                    blocks = message.get("content")
                    if not isinstance(blocks, list):
                        continue
                    for block in blocks:
                        if block.get("type") == "tool_use":
                            pending.add(str(block["id"]))
                        elif block.get("type") == "tool_result":
                            pending.discard(str(block["tool_use_id"]))
                for tool_use_id in pending:
                    context.add_tool_result(
                        tool_use_id, "Tool execution interrupted.", is_error=True,
                    )
                try:
                    context.flush()
                except Exception:
                    context.mark_failed("persistence_error")
                    logging.getLogger(__name__).exception("history persistence failed")
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        logging.getLogger(__name__).exception("browser cleanup failed")
                        context.mark_failed("cleanup_error")

            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    ts=_now(),
                )
            )

        if cancelled:
            raise asyncio.CancelledError()

        return RunOutcome(
            status=context.status,
            result=context.result,
            reason=context.reason,
        )
