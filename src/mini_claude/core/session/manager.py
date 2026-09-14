from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mini_claude.core.bus.envelope import HandlerError
from mini_claude.core.bus.events import (
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionMessageReceivedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
    SkillInvokedEvent,
)
from mini_claude.core.events.bus import EventBus
from mini_claude.core.runs import new_run_id
from mini_claude.core.session.model import PermissionMode, Session, SessionMode
from mini_claude.core.session.store import SessionStore
from mini_claude.core.skills.loader import SkillLoader

if TYPE_CHECKING:
    from mini_claude.core.llm.base import LLMProvider
    from mini_claude.core.permissions.manager import PermissionManager
    from mini_claude.core.runner import AgentRunner
    from mini_claude.core.subagent.registry import BackgroundTaskRegistry

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionManager:
    # 初始化会话管理器，接入文件存储、runner 工厂、事件总线和可选的 LLM provider（用于手动压缩）
    def __init__(
        self,
        store: SessionStore,
        runner_factory: Callable[[], AgentRunner],
        bus: EventBus,
        provider: LLMProvider | None = None,
        *,
        project_path: Path | None = None,
        default_model: str = "",
        permission_manager: PermissionManager | None = None,
        provider_factory: Callable[[str], LLMProvider] | None = None,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._provider_factory = provider_factory
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._skill_loader = SkillLoader()
        self.project_path = str((project_path or Path.cwd()).resolve())
        self._default_model = default_model
        self._permission_manager = permission_manager
        self._active: dict[str, asyncio.Task[Any]] = {}
        self._background: dict[str, BackgroundTaskRegistry] = {}
        self._closing: set[str] = set()
        self._cancelled: set[str] = set()
        self._last_cancelled: dict[str, bool] = {}
        self._outcomes: dict[str, tuple[str | None, str | None]] = {}
        self._stopping: set[str] = set()
        self._cancel_locks: dict[str, asyncio.Lock] = {}
        for session in self._store.list_sessions():
            if session.project_path != self.project_path:
                continue
            if (
                session.status not in ("active", "waiting_for_input", "closed")
                or session.mode not in ("chat", "one_shot")
            ):
                continue
            if session.permission_mode not in ("ask", "read_only", "full_access"):
                session.permission_mode = "ask"
            if session.status == "active":
                session.status = "waiting_for_input"
            session.model = session.model or default_model
            self._sessions[session.id] = session
            self._locks[session.id] = asyncio.Lock()

    # 创建新 session 并写入 meta.json
    async def create(
        self, mode: SessionMode, title: str = "", *, model: str | None = None,
        permission_mode: PermissionMode = "ask",
    ) -> Session:
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        ts = _now()
        session = Session(
            id=sid,
            mode=mode,
            status="active",
            title=title,
            created_at=ts,
            updated_at=ts,
            run_ids=[],
            project_path=self.project_path,
            model=model or self._default_model,
            permission_mode=permission_mode,
        )
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        self._store.write_meta(session)
        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        return session

    # 处理用户消息，追加 thread 并启动一次 agent run
    async def send_message(
        self, sid: str, content: str, *, run_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked() or sid in self._stopping or sid in self._closing:
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            if session.status == "waiting_for_input":
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))

            user_content: Any = content
            if attachments:
                user_content = [{"type": "text", "text": content}] if content.strip() else []
                for attachment in attachments:
                    user_content.append({
                        "type": "image", "source": {
                            "type": "base64", "media_type": attachment["media_type"],
                            "data": attachment["data"],
                        },
                    })
            self._store.append_message(sid, "user", user_content)
            await self._bus.publish(
                SessionMessageReceivedEvent(session_id=sid, content=content, ts=_now())
            )

            if not session.title:
                session.title = content[:40] or "图片对话"

            run_id = run_id or new_run_id()
            self._bus.register_run(run_id, sid)
            session.status = "active"
            self._last_cancelled[sid] = False
            self._outcomes[sid] = (None, None)
            session.run_ids.append(run_id)
            session.updated_at = _now()
            self._store.write_meta(session)

            # Skill 解析：检测 "/" 前缀，展开为系统提示覆盖和工具白名单
            goal = content or "Describe and analyze the attached images."
            system_prompt_override: str | None = None
            tool_whitelist: list[str] | None = None
            if content.startswith("/") and content[1:].strip():
                parts = content[1:].split(None, 1)
                skill_name = parts[0]
                arguments = parts[1] if len(parts) > 1 else ""
                skill = self._skill_loader.resolve(skill_name)
                if skill is not None:
                    goal = self._skill_loader.render_prompt(skill, arguments)
                    system_prompt_override = goal
                    tool_whitelist = skill.allowed_tools or None
                    await self._bus.publish(
                        SkillInvokedEvent(
                            skill_name=skill_name,
                            arguments=arguments,
                            run_id=run_id,
                            ts=_now(),
                        )
                    )

            runner = self._runner_factory()
            from mini_claude.core.subagent.registry import BackgroundTaskRegistry

            background = self._background.setdefault(sid, BackgroundTaskRegistry())
            if bind_registry := getattr(runner, "set_task_registry", None):
                bind_registry(background)
            if self._permission_manager is not None:
                self._permission_manager.set_session_mode(sid, session.permission_mode)
            task = asyncio.create_task(runner.run_and_capture(
                goal,
                run_id=run_id,
                session=session,
                store=self._store,
                system_prompt_override=system_prompt_override,
                tool_whitelist=tool_whitelist,
            ))
            self._active[sid] = task
            try:
                outcome = await task
                self._outcomes[sid] = (outcome.status, outcome.reason)
            except asyncio.CancelledError:
                if sid not in self._cancelled:
                    raise
                self._last_cancelled[sid] = True
                self._outcomes[sid] = ("failed", "cancelled")
            finally:
                self._active.pop(sid, None)
                self._cancelled.discard(sid)
                session.status = "waiting_for_input"
                session.updated_at = _now()
                self._store.write_meta(session)

            session.updated_at = _now()
            if session.mode == "one_shot":
                await background.cancel(close=True)
                self._background.pop(sid, None)
                session.status = "closed"
                await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))
            else:
                session.status = "waiting_for_input"
                await self._bus.publish(
                    SessionWaitingForInputEvent(
                        session_id=sid,
                        last_run_id=run_id,
                        ts=session.updated_at,
                    )
                )
            self._store.write_meta(session)
            return run_id

    # 返回当前项目的会话，按最后更新时间从新到旧排列
    def list_sessions(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda session: session.updated_at, reverse=True)

    # 仅定位当前项目所属会话的运行事件，避免跨项目历史泄露
    def events_path(self, run_id: str) -> Path | None:
        for session in self._sessions.values():
            if run_id in session.run_ids:
                path = self._store.runs_dir(session.id) / run_id / "events.jsonl"
                if path.resolve().is_relative_to(self._store.session_dir(session.id).resolve()):
                    return path
        return None

    # 检查所有主运行与后台子代理是否仍在执行
    def has_active_runs(self) -> bool:
        return any(lock.locked() for lock in self._locks.values()) or bool(self._active) or any(
            registry.is_running() for registry in self._background.values()
        )

    # 返回最近一次消息是否由用户停止
    def was_cancelled(self, sid: str) -> bool:
        return self._last_cancelled.get(sid, False)

    # 返回最近一次运行的真实终态，供计划任务区分模型失败与成功
    def last_outcome(self, sid: str) -> tuple[str | None, str | None]:
        return self._outcomes.get(sid, (None, None))

    # 返回会话当前是否正在运行，以便桌面重连时恢复停止按钮和运行映射
    def is_running(self, sid: str) -> bool:
        registry = self._background.get(sid)
        return sid in self._active or (registry is not None and registry.is_running())

    # 重命名会话并持久化新的标题
    async def rename(self, sid: str, title: str) -> Session:
        session = self._get_session(sid)
        if not title.strip():
            raise HandlerError(-32602, "title cannot be empty")
        session.title = title.strip()
        session.updated_at = _now()
        self._store.write_meta(session)
        return session

    # 更新下一轮使用的模型与权限模式，运行期间不允许改变授权语义
    async def configure(
        self, sid: str, *, model: str | None = None,
        permission_mode: PermissionMode | None = None,
    ) -> Session:
        session = self._get_session(sid)
        if self._locks[sid].locked() or self.is_running(sid):
            raise HandlerError(SESSION_BUSY, "session busy")
        if model is not None:
            session.model = model
        if permission_mode is not None:
            session.permission_mode = permission_mode
        session.updated_at = _now()
        self._store.write_meta(session)
        return session

    # 停止当前主运行及全部后台子代理，并清除待审批请求
    async def cancel(self, sid: str) -> bool:
        self._get_session(sid)
        async with self._cancel_locks.setdefault(sid, asyncio.Lock()):
            self._stopping.add(sid)
            background = self._background.get(sid)
            if background is not None:
                background.accepting = False
            try:
                task = self._active.get(sid)
                cancelled = task is not None and not task.done()
                if cancelled and task is not None:
                    self._cancelled.add(sid)
                    if not task.cancelling():
                        task.cancel()
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
                if background is not None:
                    cancelled = await background.cancel(close=sid in self._closing) or cancelled
                if self._permission_manager is not None:
                    self._permission_manager.cancel_session(sid, reason="user_cancelled")
                return cancelled
            finally:
                self._stopping.discard(sid)

    # 阻止新任务并取消全部活跃运行后删除会话和历史
    async def delete(self, sid: str) -> None:
        self._get_session(sid)
        lock = self._locks[sid]
        self._closing.add(sid)
        try:
            await self.cancel(sid)
            async with lock:
                self._store.delete(sid)
                del self._sessions[sid]
                self._locks.pop(sid, None)
                self._background.pop(sid, None)
                self._last_cancelled.pop(sid, None)
                self._outcomes.pop(sid, None)
        finally:
            self._closing.discard(sid)

    # 关闭守护进程前停止所有会话，避免后台工具残留
    async def stop_all(self) -> None:
        for sid in list(self._sessions):
            await self.cancel(sid)

    # 关闭前禁止新任务并等待主运行和后台任务全部结束
    async def close(self, sid: str) -> None:
        session = self._get_session(sid)
        lock = self._locks[sid]
        self._closing.add(sid)
        try:
            await self.cancel(sid)
            async with lock:
                self._background.pop(sid, None)
                session.status = "closed"
                session.updated_at = _now()
                self._store.write_meta(session)
                await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))
        finally:
            self._closing.discard(sid)

    # 手动压缩模型上下文，原始 thread 始终保留供历史展示
    async def compact(self, sid: str, focus: str = "") -> Any:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        if self._provider is None and self._provider_factory is None:
            raise HandlerError(-32020, "provider not available for compaction")
        async with lock:
            from mini_claude.core.bus.commands import SessionCompactResult
            from mini_claude.core.compact.compactor import Compactor
            messages = self._store.read_messages(sid)
            session_dir = self._store.session_dir(sid)
            compactor = Compactor(self._bus, session_dir, sid)
            provider = (
                self._provider_factory(session.model or self._default_model)
                if self._provider_factory is not None else self._provider
            )
            assert provider is not None
            result = await compactor.compact_messages(messages, provider, focus=focus)
            if result is None:
                raise HandlerError(-32021, "compaction failed or not beneficial")
            self._store.write_compacted(sid, [
                {"role": "user", "content": result.summary_text},
            ])
            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                saved_tokens=max(0, result.original_token_estimate - result.summary_tokens),
            )

    # 读取指定 session 的完整 thread 历史
    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        self._get_session(sid)
        return self._store.read_history(sid)

    # 从内存索引取 session，不存在时抛 JSON-RPC 结构化错误
    def _get_session(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            raise HandlerError(SESSION_NOT_FOUND, "session not found")
        return session
