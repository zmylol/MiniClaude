from __future__ import annotations

import asyncio
import datetime
import fnmatch
import json
import logging
import os
import re
import signal
import time
from datetime import UTC
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import mini_claude
from mini_claude.core.bus.commands import (
    AgentRunCommand,
    AgentRunResult,
    ConfigModelsCommand,
    ConfigModelsResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    ModelOption,
    PermissionRespondCommand,
    PermissionRespondResult,
    PongResult,
    SessionCancelCommand,
    SessionCancelResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionCompactCommand,
    SessionCompactResult,
    SessionConfigureCommand,
    SessionCreateCommand,
    SessionCreateResult,
    SessionDeleteCommand,
    SessionDeleteResult,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    SessionListCommand,
    SessionListResult,
    SessionRenameCommand,
    SessionSendMessageCommand,
    SessionSendMessageResult,
    SessionSummary,
    SessionUpdateResult,
)
from mini_claude.core.bus.envelope import EventPushEnvelope, HandlerError
from mini_claude.core.config import MiniConfig, get_config
from mini_claude.core.desktop_services import ManagedMcpServers, register_plugins
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.provider import AnthropicProvider
from mini_claude.core.logging_setup import setup_logging
from mini_claude.core.mcp.server import McpServerManager
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.permissions.storage import load_policy_file
from mini_claude.core.runner import AgentRunner
from mini_claude.core.runs import events_file, new_run_id
from mini_claude.core.session import SessionManager, SessionStore
from mini_claude.core.session.model import Session
from mini_claude.core.trace.record import TraceRecord
from mini_claude.core.trace.writer import TraceWriter
from mini_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from mini_claude.core.transport.socket_server import SocketServer, get_connection_writer

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


class CoreApp:
    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._broadcaster: IpcEventBroadcaster | None = None
        self._trace: TraceWriter | None = None
        self._config: MiniConfig | None = None
        self._running_runs: set[asyncio.Task[Any]] = set()
        self._sessions: SessionManager | None = None
        self._permission_manager: PermissionManager | None = None
        self._mcp_manager: McpServerManager | None = None

    # 处理 core.ping 请求，返回服务版本、运行时长、接收时间和项目路径
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=mini_claude.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
            project_path=str(Path.cwd().resolve()),
        )

    # 将 EventBus 事件写入 trace（作为 EventBus 订阅者）
    async def _trace_event_handler(self, event: BaseModel) -> None:
        assert self._trace is not None
        event_dict = event.model_dump()
        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="CORE",
                layer="event",
                kind="event",
                run_id=event_dict.get("run_id"),
                data=event_dict,
            )
        )

    # 启动一次 agent run：异步创建 AgentRunner 并立即返回 run_id
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._sessions is not None
        self._ensure_plugins_ready()
        cmd = AgentRunCommand.model_validate(params)
        session = await self._sessions.create(mode="one_shot", title=cmd.goal[:40])
        self._ensure_plugins_ready()
        run_id = new_run_id()
        run_task = asyncio.create_task(
            self._sessions.send_message(session.id, cmd.goal, run_id=run_id)
        )
        self._running_runs.add(run_task)
        run_task.add_done_callback(self._running_runs.discard)
        return AgentRunResult(run_id=run_id)

    # 创建 chat 或 one_shot session，并返回 session_id
    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        assert self._sessions is not None
        cmd = SessionCreateCommand.model_validate(params)
        session = await self._sessions.create(
            mode=cmd.mode, title=cmd.title, model=cmd.model, permission_mode=cmd.permission_mode,
        )
        return SessionCreateResult(
            session_id=session.id, status=session.status,
            model=session.model, permission_mode=session.permission_mode,
        )

    # 向 session 发送一条用户消息并同步等待对应 run 完成
    async def _session_send_handler(self, params: dict[str, Any]) -> SessionSendMessageResult:
        assert self._sessions is not None
        self._ensure_plugins_ready()
        cmd = SessionSendMessageCommand.model_validate(params)
        run_id = await self._sessions.send_message(
            cmd.session_id, cmd.content,
            attachments=[attachment.model_dump() for attachment in cmd.attachments],
        )
        status, reason = self._sessions.last_outcome(cmd.session_id)
        return SessionSendMessageResult(
            run_id=run_id, cancelled=self._sessions.was_cancelled(cmd.session_id),
            status=status, reason=reason,
        )

    # 插件变更期间阻止新运行使用正在断开的工具连接
    def _ensure_plugins_ready(self) -> None:
        if getattr(self._mcp_manager, "changing", False):
            raise HandlerError(-32040, "插件正在更新，请稍后再运行")

    # 将会话模型映射为安全的桌面会话摘要
    def _session_summary(self, session: Session) -> SessionSummary:
        assert self._sessions is not None
        running = self._sessions.is_running(session.id)
        return SessionSummary(
            session_id=session.id, title=session.title, status=session.status,
            mode=session.mode, model=session.model, permission_mode=session.permission_mode,
            project_path=session.project_path, created_at=session.created_at,
            updated_at=session.updated_at, run_ids=list(session.run_ids), running=running,
            active_run_id=session.run_ids[-1] if running and session.run_ids else None,
            pending_permissions=(
                self._permission_manager.pending_for(session.id)
                if self._permission_manager is not None else []
            ),
        )

    # 列出当前项目的持久化会话和正在执行的状态
    async def _session_list_handler(self, params: dict[str, Any]) -> SessionListResult:
        SessionListCommand.model_validate(params)
        assert self._sessions is not None
        return SessionListResult(
            sessions=[self._session_summary(session) for session in self._sessions.list_sessions()],
            project_path=self._sessions.project_path,
        )

    # 重命名会话并返回更新后的摘要
    async def _session_rename_handler(self, params: dict[str, Any]) -> SessionUpdateResult:
        cmd = SessionRenameCommand.model_validate(params)
        assert self._sessions is not None
        session = await self._sessions.rename(cmd.session_id, cmd.title)
        return SessionUpdateResult(session=self._session_summary(session))

    # 更新会话实际使用的模型与权限模式
    async def _session_configure_handler(self, params: dict[str, Any]) -> SessionUpdateResult:
        cmd = SessionConfigureCommand.model_validate(params)
        assert self._sessions is not None
        session = await self._sessions.configure(
            cmd.session_id, model=cmd.model, permission_mode=cmd.permission_mode,
        )
        return SessionUpdateResult(session=self._session_summary(session))

    # 停止会话主运行、工具进程和子代理
    async def _session_cancel_handler(self, params: dict[str, Any]) -> SessionCancelResult:
        cmd = SessionCancelCommand.model_validate(params)
        assert self._sessions is not None
        return SessionCancelResult(cancelled=await self._sessions.cancel(cmd.session_id))

    # 删除会话持久化记录
    async def _session_delete_handler(self, params: dict[str, Any]) -> SessionDeleteResult:
        cmd = SessionDeleteCommand.model_validate(params)
        assert self._sessions is not None
        await self._sessions.delete(cmd.session_id)
        return SessionDeleteResult()

    # 返回当前配置的模型及同服务的候选项，不暴露服务凭据
    async def _config_models_handler(self, params: dict[str, Any]) -> ConfigModelsResult:
        ConfigModelsCommand.model_validate(params)
        assert self._config is not None
        current = self._config.llm.default_model
        models = [current]
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        if base_url == "https://api.anthropic.com" and current.startswith("claude-"):
            models.extend(["claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"])
        return ConfigModelsResult(
            current_model=current,
            models=[ModelOption(id=name, label=name) for name in dict.fromkeys(models)],
        )

    # 返回 session 的完整 Anthropic messages 历史
    async def _session_history_handler(self, params: dict[str, Any]) -> SessionGetHistoryResult:
        assert self._sessions is not None
        cmd = SessionGetHistoryCommand.model_validate(params)
        messages = await self._sessions.get_history(cmd.session_id)
        return SessionGetHistoryResult(messages=messages)

    # 接收客户端权限审批响应，resolve 对应挂起的 Future
    async def _permission_respond_handler(self, params: dict[str, Any]) -> PermissionRespondResult:
        cmd = PermissionRespondCommand.model_validate(params)
        logger.info(
            "permission.respond received tool_use_id=%s decision=%s",
            cmd.tool_use_id, cmd.decision,
        )
        if self._permission_manager is None:
            logger.error("permission.respond: PermissionManager not initialized")
            return PermissionRespondResult()
        try:
            self._permission_manager.respond(cmd.tool_use_id, cmd.decision, run_id=cmd.run_id)
        except ValueError as exc:
            raise HandlerError(-32602, str(exc)) from exc
        return PermissionRespondResult()

    # 手动压缩模型上下文并保存摘要检查点，原始历史保持完整
    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result  # type: ignore[no-any-return]

    # 关闭 session 并返回 closed 状态
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        cmd = SessionCloseCommand.model_validate(params)
        await self._sessions.close(cmd.session_id)
        return SessionCloseResult(status="closed")

    # 注册客户端事件订阅，可选先回放 events.jsonl 历史再接收实时流
    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        cmd = EventSubscribeCommand.model_validate(params)
        writer = get_connection_writer()

        replayed_count = 0
        if cmd.replay_from_run is not None:
            replayed_count = await self._replay_events(
                cmd.replay_from_run, writer, cmd.topics, scope=cmd.scope,
            )

        assert self._broadcaster is not None
        sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)

    # 从 events.jsonl 向 writer 回放匹配 topic 的历史事件，返回已回放条数
    async def _replay_events(
        self,
        run_id: str,
        writer: asyncio.StreamWriter,
        topics: list[str],
        *,
        scope: str = "global",
    ) -> int:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
            raise HandlerError(-32602, "invalid run id")
        path = events_file(run_id)
        session_id = None
        if self._sessions is not None:
            session_path = self._sessions.events_path(run_id)
            if session_path is None:
                return 0
            path = session_path
            session_id = next(
                (session.id for session in self._sessions.list_sessions()
                 if run_id in session.run_ids), None,
            )
        if not path.exists():
            return 0

        records: list[tuple[Path, dict[str, Any]]] = []
        candidates = {path, *path.parent.parent.glob("*/events.jsonl")}
        for candidate in sorted(candidates):
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(
                path.parent.parent.resolve()
            ):
                continue
            try:
                lines = candidate.read_bytes().split(b"\n")
            except OSError:
                continue
            for line in lines:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(event, dict):
                    records.append((candidate, event))
        allowed = {run_id}
        parents: dict[str, str] = {}
        while True:
            descendants = {
                str(event["run_id"]): str(event["parent_run_id"]) for _, event in records
                if event.get("run_id") and event.get("parent_run_id") in allowed
            }
            parents.update(descendants)
            if descendants.keys() <= allowed:
                break
            allowed.update(descendants)

        count = 0
        seen: dict[str, Path] = {}
        for source, event in sorted(records, key=lambda row: str(row[1].get("ts", ""))):
            if event.get("run_id") not in allowed:
                continue
            # 旧日志只有开始事件携带父关系，先补齐已确认归属再执行同一作用域过滤
            event = {
                **event,
                "session_id": event.get("session_id") or session_id,
                "root_run_id": event.get("root_run_id") or run_id,
                "parent_run_id": event.get("parent_run_id") or parents.get(event["run_id"]),
            }
            if not IpcEventBroadcaster._matches_scope(
                event.get("run_id"), scope, event.get("session_id"), event.get("root_run_id"),
            ):
                continue
            identity = json.dumps(event, sort_keys=True)
            if identity in seen and seen[identity] != source:
                continue
            seen[identity] = source
            event_type: str = event.get("type", "")
            if not any(fnmatch.fnmatch(event_type, p) for p in topics):
                continue
            envelope = EventPushEnvelope(event=event)
            writer.write(envelope.model_dump_json().encode() + b"\n")
            count += 1

        if count:
            await writer.drain()
        return count

    # 启动守护进程：加载配置、初始化日志、启动 trace、启动 TCP 服务器，并等待退出信号
    async def run(self) -> None:
        self._start_time = time.monotonic()
        self._config = get_config()
        setup_logging(self._config)

        if self._config.trace.enabled:
            trace_path = Path(self._config.trace.file).expanduser()
            self._trace = TraceWriter(trace_path)
            await self._trace.start()
            self._bus.subscribe(self._trace_event_handler)

        policy_file = Path("~/.mini/policy.toml").expanduser()
        self._permission_manager = PermissionManager(
            policy_file=policy_file,
            timeout_s=self._config.permission.timeout_s,
        )
        logger.info(
            "permission manager: timeout_s=%.1f  persistent=%d entries",
            self._config.permission.timeout_s,
            len(load_policy_file(policy_file)),
        )

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._bus.subscribe(self._broadcaster.handle)
        sessions_root = Path("~/.mini/sessions").expanduser()
        store = SessionStore(sessions_root)
        assert self._config is not None

        self._mcp_manager = ManagedMcpServers(
            Path.cwd(),
            is_busy=lambda: bool(self._running_runs)
            or bool(self._sessions and self._sessions.has_active_runs()),
        )
        if self._config.mcp.servers:
            logger.info("mcp: starting %d server(s)", len(self._config.mcp.servers))
        await self._mcp_manager.start_all(self._config.mcp.servers)

        self._sessions = SessionManager(
            store,
            runner_factory=lambda: AgentRunner(
                self._config,  # type: ignore[arg-type]
                bus=self._bus,
                trace=self._trace,
                permission_manager=self._permission_manager,
                mcp_manager=self._mcp_manager,
            ),
            bus=self._bus,
            provider_factory=AnthropicProvider,
            project_path=Path.cwd(),
            default_model=self._config.llm.default_model,
            permission_manager=self._permission_manager,
        )

        server = SocketServer(
            self._config.host,
            self._config.port,
            self._broadcaster,
            trace=self._trace,
        )
        server.register("core.ping", self._ping_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("event.subscribe", self._subscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.send_message", self._session_send_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.close", self._session_close_handler)
        server.register("permission.respond", self._permission_respond_handler)
        server.register("session.compact", self._session_compact_handler)
        server.register("session.list", self._session_list_handler)
        server.register("session.rename", self._session_rename_handler)
        server.register("session.configure", self._session_configure_handler)
        server.register("session.cancel", self._session_cancel_handler)
        server.register("session.delete", self._session_delete_handler)
        server.register("config.models", self._config_models_handler)
        register_plugins(server, self._mcp_manager)

        addr = await server.start()
        logger.info("mini-core %s listening addr=%s", mini_claude.__version__, addr)
        logger.info("config: %s", self._config)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        loop.add_signal_handler(signal.SIGINT, shutdown.set)
        loop.add_signal_handler(signal.SIGTERM, shutdown.set)

        await shutdown.wait()

        logger.info("shutting down")
        await self._sessions.stop_all()
        for run_task in list(self._running_runs):
            run_task.cancel()
        if self._running_runs:
            await asyncio.gather(*self._running_runs, return_exceptions=True)
        if self._mcp_manager is not None:
            await self._mcp_manager.stop_all()
        await server.stop()
        if self._trace is not None:
            await self._trace.stop()


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    asyncio.run(CoreApp().run())
