"""Exercise same-turn tool concurrency with real subprocesses and no API calls."""

from __future__ import annotations

import json
import shlex
import sys
from copy import deepcopy
from pathlib import Path

from mini_claude.core.context import ExecutionContext
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.types import LlmResponse, ToolCallBlock
from mini_claude.core.loop import AgentLoop
from mini_claude.core.tools.builtin.bash import BashTool
from mini_claude.core.tools.registry import ToolRegistry

_RENDEZVOUS_SCRIPT = """
import json
import sys
import time
from pathlib import Path

own, peer = map(Path, sys.argv[1:3])
own.touch()
deadline = time.monotonic() + 3
while not peer.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
print(json.dumps({"tool": own.stem, "overlap": peer.exists()}))
"""


class _ParallelBashProvider:
    def __init__(self, calls: list[ToolCallBlock]) -> None:
        self.calls = calls
        self.chat_count = 0
        self.summary_messages: list[dict[str, object]] = []

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        self.chat_count += 1
        if self.chat_count == 1:
            return LlmResponse(stop_reason="tool_use", tool_calls=self.calls)
        self.summary_messages = deepcopy(messages)
        return LlmResponse(stop_reason="end_turn", text="both tools finished")


async def test_same_turn_bash_processes_overlap_and_return_ordered_results(
    tmp_path: Path,
) -> None:
    # 两个子进程先声明已启动，再等待对方；串行执行会使第一个返回 overlap=false。
    markers = [tmp_path / "first.started", tmp_path / "second.started"]
    calls = [
        ToolCallBlock(
            id=f"call_{own.stem}",
            name="bash",
            input={
                "command": shlex.join(
                    [sys.executable, "-c", _RENDEZVOUS_SCRIPT, str(own), str(peer)]
                ),
                "timeout": 10,
            },
        )
        for own, peer in [markers, markers[::-1]]
    ]
    provider = _ParallelBashProvider(calls)
    registry = ToolRegistry()
    registry.register(BashTool())
    context = ExecutionContext(run_id="parallel-bash", goal="run both tools", max_steps=2)

    await AgentLoop(provider, registry, EventBus()).run(context)

    assert context.status == "success"
    assert context.result == "both tools finished"
    assert provider.chat_count == 2
    # 第二次模型调用必须看到同一条 user 消息中按原始调用顺序排列的全部结果。
    assert len(provider.summary_messages) == 3
    result_message = provider.summary_messages[-1]
    assert result_message["role"] == "user"
    results = result_message["content"]
    assert isinstance(results, list)
    assert [result["tool_use_id"] for result in results] == [call.id for call in calls]
    assert all(result["type"] == "tool_result" for result in results)
    assert all(not result.get("is_error", False) for result in results)
    assert [json.loads(result["content"]) for result in results] == [
        {"tool": "first", "overlap": True},
        {"tool": "second", "overlap": True},
    ]
