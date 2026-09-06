from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TextIO

from pydantic import BaseModel, ConfigDict, Field

from mini_claude.core.bus.envelope import HandlerError
from mini_claude.core.bus.schedule_commands import (
    Schedule,
    ScheduleChangedEvent,
    ScheduleCreateCommand,
    ScheduleDeleteCommand,
    ScheduleDeleteResult,
    ScheduleResult,
    ScheduleRunNowCommand,
    SchedulesListCommand,
    SchedulesListResult,
    ScheduleUpdateCommand,
)
from mini_claude.core.config import MiniConfig

logger = logging.getLogger(__name__)
SCHEDULE_IN_USE = -32053


class Workspace(Protocol):
    project_path: Path
    project_selected: bool
    config: MiniConfig

    # 检查计划所属项目是否仍在桌面列表中。
    def has_project(self, path: Path) -> bool: ...

    # 向指定项目所属的 core 发送命令，避免切换项目改变已创建计划的目标。
    async def request_core_for(
        self, project_path: Path, method: str, params: dict[str, Any],
    ) -> dict[str, Any]: ...


class ScheduleStore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedules: list[Schedule] = Field(default_factory=list)


# 返回含时区的当前时间，方便调度测试注入固定时钟。
def utc_now() -> datetime:
    return datetime.now(UTC)


class DesktopServices:
    # 维护本次应用访问过的项目计划，每个项目的任务保持独立持久化。
    def __init__(
        self, workspace: Workspace, *, now: Callable[[], datetime] = utc_now,
        on_change: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.workspace = workspace
        self._now = now
        self._on_change = on_change
        self._jobs: dict[Path, dict[str, Schedule]] = {}
        self._leases: dict[Path, TextIO] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._submissions: set[asyncio.Task[Any]] = set()

    # 只拦截网关本地计划命令，其余操作继续由 core 处理。
    def handles(self, method: str) -> bool:
        return method in {
            "schedules.list", "schedules.create", "schedules.update",
            "schedules.delete", "schedules.run_now",
        }

    # 启动应用存活期间的调度循环，不安装系统后台任务。
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="desktop-schedules")

    # 关闭调度及未确认的提交请求，已发送的任务不会自动重试。
    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        tasks = list(self._submissions)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            for lease in self._leases.values():
                lease.close()
            self._leases.clear()
            self._jobs.clear()

    # 移除项目时拒绝正在执行的计划，仅释放内存与锁而保留磁盘任务记录。
    async def detach_project(self, project: Path) -> None:
        async with self._lock:
            if any(job.status in {"submitting", "running"}
                   for job in self._jobs.get(project, {}).values()):
                raise ValueError("项目中有正在运行的计划，请先停止任务再移除。")
            self._jobs.pop(project, None)
            lease = self._leases.pop(project, None)
            if lease is not None:
                lease.close()

    # 定期检查计划，单次错误写入日志并保留后续检查机会。
    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("桌面计划检查失败")
            await asyncio.sleep(1)

    # 读取项目计划，将上次进程中断的提交标记为中断而不是重复发起。
    def _load(self, project: Path) -> dict[str, Schedule]:
        if project in self._jobs:
            return self._jobs[project]
        self._claim_project(project)
        path = project / ".mini" / "desktop_schedules.json"
        saved = ScheduleStore()
        if path.exists():
            try:
                saved = ScheduleStore.model_validate_json(path.read_text())
            except (OSError, ValueError) as exc:
                raise HandlerError(
                    -32050, "无法读取项目计划文件，请检查 desktop_schedules.json",
                ) from exc
        jobs = {job.id: job for job in saved.schedules}
        recovered = False
        for job in jobs.values():
            if job.status in {"submitting", "running"}:
                job.status = "interrupted"
                job.last_error = "上次应用退出时提交尚未确认，不会自动重试本次任务。"
                self._advance(job, self._now())
                recovered = True
        if recovered:
            self._persist(project, jobs)
        else:
            self._jobs[project] = jobs
        return jobs

    # 对每个项目持有独占进程锁，第二个网关不得调度、恢复或覆盖活动所有者的数据。
    def _claim_project(self, project: Path) -> None:
        if project in self._leases:
            return
        path = project / ".mini" / "desktop_schedules.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        lease = path.open("a", encoding="utf-8")
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lease.close()
            if isinstance(exc, BlockingIOError):
                raise HandlerError(
                    SCHEDULE_IN_USE, "此项目的计划由另一个 MiniClaude 实例管理，请关闭它后重试。",
                ) from exc
            raise
        self._leases[project] = lease

    # 在发送执行命令前完成原子持久化，防止重启重复领取同一次计划。
    def _persist(self, project: Path, jobs: dict[str, Schedule]) -> None:
        path = project / ".mini" / "desktop_schedules.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(ScheduleStore(schedules=list(jobs.values())).model_dump_json(indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._jobs[project] = jobs

    # 单次计划领取后停用，每日计划跳过错过的日期并推进到下一次未来时间。
    def _advance(self, job: Schedule, now: datetime) -> None:
        if job.repeat == "once":
            job.enabled = False
        elif job.next_run <= now:
            days = (now - job.next_run) // timedelta(days=1) + 1
            job.next_run += timedelta(days=days)

    # 查找项目中的计划并区分不存在与正在提交的操作冲突。
    def _find(self, jobs: dict[str, Schedule], job_id: str) -> Schedule:
        if job_id not in jobs:
            raise HandlerError(-32051, "计划不存在")
        job = jobs[job_id]
        if job.status in {"submitting", "running"}:
            raise HandlerError(-32052, "计划正在提交，请稍后重试")
        return job

    # 返回计划及应用运行约束，明确接收任务不代表模型已经执行完成。
    def _result(self, job: Schedule) -> dict[str, Any]:
        return ScheduleResult(schedule=job).model_dump(mode="json")

    # 发布已持久化的计划变化，界面连接异常不能改变任务执行结果。
    async def _notify(self, project: Path, changes: dict[str, Any]) -> None:
        if self._on_change is not None:
            try:
                event = ScheduleChangedEvent.model_validate({
                    "project_path": str(project), **changes,
                })
                await self._on_change(event.model_dump(mode="json", exclude_none=True))
            except Exception:
                logger.exception("桌面计划状态通知失败")

    # 处理计划列表、创建、编辑、删除及立即运行命令。
    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.workspace.project_selected:
            raise ValueError("请先选择或打开一个项目。")
        project = self.workspace.project_path.resolve()
        if method == "schedules.run_now":
            command_id = ScheduleRunNowCommand.model_validate(params)
            return self._result(await self._launch(project, command_id.id))
        async with self._lock:
            jobs = self._load(project)
            if method == "schedules.list":
                SchedulesListCommand.model_validate(params)
                return SchedulesListResult(schedules=list(jobs.values())).model_dump(mode="json")
            if method == "schedules.create":
                command = ScheduleCreateCommand.model_validate(params)
                job = Schedule(
                    id=f"schedule-{uuid.uuid4().hex[:12]}",
                    **command.model_dump(exclude={"type"}),
                )
                self._persist(project, {**jobs, job.id: job})
                result = self._result(job)
            elif method == "schedules.update":
                update = ScheduleUpdateCommand.model_validate(params)
                original = self._find(jobs, update.id)
                changes = update.model_dump(exclude_unset=True, exclude={"id", "type"})
                job = Schedule.model_validate({**original.model_dump(), **changes})
                self._persist(project, {**jobs, job.id: job})
                result = self._result(job)
            elif method == "schedules.delete":
                deleting = ScheduleDeleteCommand.model_validate(params)
                self._find(jobs, deleting.id)
                remaining = {key: job for key, job in jobs.items() if key != deleting.id}
                self._persist(project, remaining)
                result = ScheduleDeleteResult(deleted=True, id=deleting.id).model_dump()
            else:
                raise HandlerError(-32601, "未知计划命令")
        await self._notify(project, result)
        return result

    # 同时检查已访问的项目，旧项目计划继续绑定原项目的 core 地址。
    async def tick(self) -> None:
        async with self._lock:
            try:
                if self.workspace.project_selected:
                    self._load(self.workspace.project_path.resolve())
            except HandlerError as exc:
                if exc.code != SCHEDULE_IN_USE:
                    raise
            now = self._now()
            due = [
                (project, job.id) for project, jobs in self._jobs.items() for job in jobs.values()
                if self.workspace.has_project(project)
                and job.enabled and job.status not in {"submitting", "running"}
                and job.next_run <= now
            ]
        if due:
            await asyncio.gather(*(
                self._launch(project, job_id, scheduled=True) for project, job_id in due
            ))

    # 后台运行计划，在会话创建完成后立即返回，避免阻塞审批和停止命令。
    async def _launch(self, project: Path, job_id: str, *, scheduled: bool = False) -> Schedule:
        ready: asyncio.Future[Schedule] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._submit(project, job_id, ready, scheduled=scheduled))
        self._submissions.add(task)

        # 回收后台任务并传播创建失败，已返回界面的后续失败由状态事件报告。
        def finished(completed: asyncio.Task[Schedule]) -> None:
            self._submissions.discard(completed)
            if completed.cancelled():
                if not ready.done():
                    ready.cancel()
                return
            error = completed.exception()
            if not ready.done():
                if error is not None:
                    ready.set_exception(error)
                else:
                    ready.set_result(completed.result())
            elif error is not None:
                logger.error(
                    "桌面计划后台任务失败", exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(finished)
        return await ready

    # 先持久领取再创建会话和发送消息，任何不确定结果都不自动补发。
    async def _submit(
        self, project: Path, job_id: str, ready: asyncio.Future[Schedule], *, scheduled: bool,
    ) -> Schedule:
        async with self._lock:
            if not self.workspace.has_project(project):
                raise ValueError("项目已移除，计划不会继续执行。")
            jobs = self._load(project)
            current = jobs.get(job_id)
            if scheduled and current is not None and (
                not current.enabled or current.next_run > self._now()
                or current.status in {"submitting", "running"}
            ):
                return current
            job = self._find(jobs, job_id).model_copy(deep=True)
            job.status = "submitting"
            job.last_run = self._now()
            job.last_error = None
            job.last_session_id = None
            self._advance(job, job.last_run)
            self._persist(project, {**jobs, job.id: job})
        try:
            session = await asyncio.wait_for(
                self.workspace.request_core_for(
                    project, "session.create", {"mode": "chat", "title": job.title},
                ), timeout=10,
            )
            session_id = session.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise RuntimeError("core 未返回有效会话编号")
            job.last_session_id = session_id
            job.status = "running"
            async with self._lock:
                self._persist(project, {**self._jobs[project], job.id: job})
            await self._notify(project, self._result(job))
            if not ready.done():
                ready.set_result(job.model_copy(deep=True))
            outcome = await self.workspace.request_core_for(
                project, "session.send_message", {"session_id": session_id, "content": job.prompt},
            )
            if outcome.get("cancelled") or outcome.get("status") == "failed":
                job.status = "error"
                job.last_error = str(outcome.get("reason") or "任务已取消")
            else:
                job.status = "success"
        except asyncio.CancelledError:
            job.status = "interrupted"
            job.last_error = "应用关闭时提交尚未确认，不会自动重试本次任务。"
            raise
        except Exception as exc:
            job.status = "error"
            job.last_error = f"提交失败或未确认，不会自动重试：{str(exc)[:400]}"
        finally:
            async with self._lock:
                self._persist(project, {**self._jobs[project], job.id: job})
            await self._notify(project, self._result(job))
        return job
