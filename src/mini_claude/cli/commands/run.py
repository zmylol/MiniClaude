from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

from mini_claude.core.config import MiniConfig
from mini_claude.core.transport.socket_client import IpcError, SocketClient


class ResponseBuffer:
    # 按运行与步骤缓冲临时 token，避免无法撤回的 stdout 出现失败尝试的残句
    def __init__(self) -> None:
        self._texts: dict[tuple[str, int], str] = {}
        self._steps: dict[str, int] = {}
        self._settled: set[tuple[str, int]] = set()

    # 只输出完整响应，并在旧事件日志的步骤或运行结束时兼容刷新未完成的 token
    def handle(self, event: dict[str, Any]) -> bool:
        kind = event.get("type", "")
        run_id = str(event.get("run_id") or "")
        step = int(event.get("step") or self._steps.get(run_id, 0))
        key = (run_id, step)
        if kind == "step.started":
            self._steps[run_id] = step
        if kind == "llm.token":
            if key not in self._settled:
                self._texts[key] = self._texts.get(key, "") + str(event.get("token", ""))
            return True
        if kind in {"llm.response.completed", "llm.response.failed"}:
            if key not in self._settled:
                self._texts.pop(key, None)
                self._settled.add(key)
                text = str(event.get("text", ""))
                if kind == "llm.response.completed" and text:
                    print(text, flush=True)
            return True
        if kind in {"step.finished", "run.finished"}:
            for pending in list(self._texts):
                if pending[0] == run_id and (kind == "run.finished" or pending[1] in {0, step}):
                    text = self._texts.pop(pending)
                    self._settled.add(pending)
                    if text:
                        print(text, flush=True)
        return False


class StdoutPrinter:
    # 接收 dict 格式的事件并将运行进度格式化打印到终端
    def __init__(self) -> None:
        self._inline = False  # True while LLM tokens are mid-line
        self._run_start: float = 0.0
        self._responses = ResponseBuffer()

    # 若当前行有未换行的 token，补一个换行符
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    # 根据事件 type 字段分发并格式化打印到 stdout/stderr
    async def handle(self, event: dict[str, Any]) -> None:
        if self._responses.handle(event):
            return
        t = event.get("type", "")

        if t == "run.started":
            self._run_start = time.monotonic()
            print(f"[run] {event.get('run_id', '')}")

        elif t == "step.started":
            self._ensure_newline()
            print(f"[step {event.get('step')}] planning...")

        elif t == "tool.call_started":
            self._ensure_newline()
            params_str = json.dumps(event.get("params", {}), ensure_ascii=False)
            print(f"[tool] {event.get('tool_name', '')} {params_str}")

        elif t == "tool.call_finished":
            print(f"[tool] {event.get('tool_name', '')} ✓  {event.get('elapsed_ms')}ms")

        elif t == "tool.call_failed":
            print(
                f"[tool] {event.get('tool_name', '')} ✗  {event.get('error_message', '')}",
                file=sys.stderr,
            )

        elif t == "step.finished":
            self._ensure_newline()
            print(f"[step {event.get('step')}] done")

        elif t == "run.finished":
            self._ensure_newline()
            elapsed = time.monotonic() - self._run_start
            print(f"[run] {event.get('status', '')}  {event.get('steps')} steps  {elapsed:.1f}s")


# 异步核心：连接 daemon，订阅事件，触发 run，等待 run.finished
async def _run_async(goal: str, config: MiniConfig) -> int:
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    printer = StdoutPrinter()
    finished = asyncio.Event()
    exit_code = 0
    owned_run: str | None = None
    pending_events: list[dict[str, Any]] = []
    owned_children: set[str] = set()

    # 仅打印本次调用及其子运行，回包前暂存事件以避免并发运行串线
    async def on_event(event: dict[str, Any]) -> None:
        nonlocal exit_code
        if owned_run is None:
            pending_events.append(event)
            return
        run_id = event.get("run_id")
        parent = event.get("parent_run_id")
        if parent == owned_run or parent in owned_children:
            owned_children.add(str(run_id))
        if (run_id != owned_run and event.get("root_run_id") != owned_run
                and run_id not in owned_children):
            return
        await printer.handle(event)
        if event.get("type") == "run.finished" and run_id == owned_run:
            if event.get("status") != "success":
                exit_code = 1
            finished.set()

    client.on_event(on_event)
    loop_task = asyncio.create_task(client.run_event_loop())

    try:
        await client.send_command(
            "event.subscribe",
            {
                "topics": ["run.*", "step.*", "tool.*", "llm.*", "subagent.*"],
                "scope": "global",
            },
        )
        result = await client.send_command("agent.run", {"goal": goal})
        owned_run = str(result["run_id"])
        for event in pending_events:
            await on_event(event)
        pending_events.clear()
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        loop_task.cancel()
        await client.close()
        return 1

    await finished.wait()

    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    await client.close()
    return exit_code


# 执行 mini run --goal "..." 命令
def cmd_run(goal: str, config: MiniConfig) -> None:
    try:
        exit_code = asyncio.run(_run_async(goal, config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
