from __future__ import annotations

import asyncio
import json
import sys
import time

from pydantic import BaseModel

from mini_claude.core.bus.events import (
    LlmTokenEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    ToolCallFailedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from mini_claude.core.config import MiniConfig
from mini_claude.core.runner import AgentRunner


class StdoutPrinter:
    # 订阅事件总线并将运行进度格式化打印到终端
    def __init__(self) -> None:
        self._inline = False  # True while LLM tokens are mid-line
        self._run_start: float = 0.0

    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    async def handle(self, event: BaseModel) -> None:
        if isinstance(event, RunStartedEvent):
            self._run_start = time.monotonic()
            print(f"[run] {event.run_id}")

        elif isinstance(event, StepStartedEvent):
            self._ensure_newline()
            print(f"[step {event.step}] planning...")

        elif isinstance(event, LlmTokenEvent):
            print(event.token, end="", flush=True)
            self._inline = True

        elif isinstance(event, ToolCallStartedEvent):
            self._ensure_newline()
            params_str = json.dumps(event.params, ensure_ascii=False)
            print(f"[tool] {event.tool_name} {params_str}")

        elif isinstance(event, ToolCallFinishedEvent):
            print(f"[tool] {event.tool_name} ✓  {event.elapsed_ms}ms")

        elif isinstance(event, ToolCallFailedEvent):
            print(
                f"[tool] {event.tool_name} ✗  {event.error_message}",
                file=sys.stderr,
            )

        elif isinstance(event, StepFinishedEvent):
            self._ensure_newline()
            print(f"[step {event.step}] done")

        elif isinstance(event, RunFinishedEvent):
            self._ensure_newline()
            elapsed = time.monotonic() - self._run_start
            print(f"[run] {event.status}  {event.steps} steps  {elapsed:.1f}s")


# 执行 mini run --goal "..." 命令
def cmd_run(goal: str, config: MiniConfig) -> None:
    printer = StdoutPrinter()
    runner = AgentRunner(config, extra_handlers=[printer.handle])
    try:
        asyncio.run(runner.run(goal))
    except KeyboardInterrupt:
        sys.exit(130)
