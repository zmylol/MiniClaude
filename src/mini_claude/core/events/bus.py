from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel

type EventHandler = Callable[[BaseModel], Awaitable[None]]


class EventBus:
    # 初始化订阅者和运行归属，归属在首个运行事件前注册
    def __init__(self) -> None:
        self._subscribers: list[EventHandler] = []
        self._runs: dict[str, dict[str, str | None]] = {}

    # 注册会话和父子关系，使任何步骤事件都能独立恢复归属
    def register_run(
        self, run_id: str, session_id: str | None = None,
        parent_run_id: str | None = None, root_run_id: str | None = None,
    ) -> None:
        parent = self._runs.get(parent_run_id or "", {})
        previous = self._runs.get(run_id, {})
        self._runs[run_id] = {
            "session_id": session_id or previous.get("session_id") or parent.get("session_id"),
            "parent_run_id": parent_run_id or previous.get("parent_run_id"),
            "root_run_id": root_run_id or previous.get("root_run_id")
                           or parent.get("root_run_id") or parent_run_id or run_id,
        }

    # 读取运行作用域，供独立子事件总线继承根运行关系
    def run_scope(self, run_id: str) -> dict[str, str | None]:
        return dict(self._runs.get(run_id, {}))

    # 注册一个事件处理函数
    def subscribe(self, handler: EventHandler) -> None:
        self._subscribers.append(handler)

    # 释放已结束运行的事件订阅，避免持有旧 writer 和 runner
    def unsubscribe(self, handler: EventHandler) -> None:
        self._subscribers = [
            subscriber for subscriber in self._subscribers if subscriber != handler
        ]

    # 按注册顺序依次调用所有订阅者
    async def publish(self, event: BaseModel) -> None:
        from mini_claude.core.bus.events import RunEvent, RunStartedEvent, SubagentStartedEvent

        if isinstance(event, (RunStartedEvent, SubagentStartedEvent)):
            self.register_run(
                event.run_id, event.session_id, event.parent_run_id, event.root_run_id,
            )
        if isinstance(event, RunEvent):
            scope = self.run_scope(str(getattr(event, "run_id", "")))
            if scope:
                event = event.model_copy(update=scope)
        for handler in list(self._subscribers):
            await handler(event)
