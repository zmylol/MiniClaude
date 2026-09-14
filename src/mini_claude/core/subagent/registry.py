from __future__ import annotations

import asyncio
from dataclasses import dataclass

from mini_claude.core.context import ExecutionContext


# 管理后台 subagent 任务的生命周期：注册、查询、批量取消
@dataclass
class BackgroundResult:
    status: str
    result: str = ""
    reason: str | None = None


class BackgroundTaskRegistry:
    # 保存会话内活跃任务和轻量结果，避免长期保留已完成执行上下文
    def __init__(self) -> None:
        self._tasks: dict[str, tuple[asyncio.Task[None], ExecutionContext]] = {}
        self._results: dict[str, BackgroundResult] = {}
        self.accepting = True

    # 注册一个后台任务及其执行上下文
    def register(
        self,
        run_id: str,
        task: asyncio.Task[None],
        context: ExecutionContext,
    ) -> None:
        if not self.accepting:
            task.cancel()
            raise RuntimeError("session is stopping")
        self._tasks[run_id] = (task, context)
        task.add_done_callback(lambda _: self._finish(run_id))

    # 将协程异常和业务状态归并为结果，并释放完整上下文
    def _finish(self, run_id: str) -> None:
        entry = self._tasks.get(run_id)
        if entry is None or not entry[0].done():
            return
        task, context = self._tasks.pop(run_id)
        if task.cancelled() or context.reason == "cancelled":
            result = BackgroundResult("cancelled", context.result, "cancelled")
        elif (error := task.exception()) is not None:
            result = BackgroundResult("failed", context.result, str(error))
        else:
            status = context.status if context.status != "running" else "failed"
            result = BackgroundResult(status, context.result, context.reason)
        self._results[run_id] = result

    # 查询运行中状态或已完成的轻量结果
    def result(self, run_id: str) -> BackgroundResult | None:
        self._finish(run_id)
        if run_id in self._tasks:
            return BackgroundResult("running")
        return self._results.get(run_id)

    # 返回当前是否有尚未完成的后台任务
    def is_running(self) -> bool:
        return any(not task.done() for task, _ in self._tasks.values())

    # 暂停派生后取消并等待全部活跃任务，普通停止保留结果供后续查询
    async def cancel(self, *, close: bool = False) -> bool:
        self.accepting = False
        tasks = [task for task, _ in self._tasks.values() if not task.done()]
        try:
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for run_id in list(self._tasks):
                self._finish(run_id)
            if close:
                self._results.clear()
            return bool(tasks)
        finally:
            self.accepting = not close

    # 查询后台任务及其上下文；不存在时返回 None
    def get(self, run_id: str) -> tuple[asyncio.Task[None], ExecutionContext] | None:
        return self._tasks.get(run_id)

    # 返回所有已注册的 (task, context) 对，用于 daemon 退出时批量清理
    def all(self) -> list[tuple[asyncio.Task[None], ExecutionContext]]:
        return list(self._tasks.values())
