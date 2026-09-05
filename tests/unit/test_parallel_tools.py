from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from pydantic import BaseModel

from mini_claude.core.bus.events import (
    PermissionRequestedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from mini_claude.core.context import ExecutionContext
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.types import LlmResponse, ToolCallBlock
from mini_claude.core.loop import AgentLoop
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.registry import ToolRegistry


class _Provider:
    def __init__(self, calls: list[ToolCallBlock]) -> None:
        self.calls = calls
        self.observed_results: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        if step == 1:
            return LlmResponse(stop_reason="tool_use", tool_calls=self.calls)
        self.observed_results = list(messages[-1]["content"])
        return LlmResponse(stop_reason="end_turn", text="done")


class _ControlledTool(BaseTool):
    name = "controlled"
    description = "Wait for the test to release this call."
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.started = {name: asyncio.Event() for name in ("first", "second")}
        self.release = {name: asyncio.Event() for name in self.started}
        self.cancelled: set[str] = set()

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        name = str(params["name"])
        self.started[name].set()
        try:
            await self.release[name].wait()
        except asyncio.CancelledError:
            self.cancelled.add(name)
            raise
        return ToolResult(content=name)


def _setup(
    *, permission_manager: PermissionManager | None = None,
) -> tuple[AgentLoop, ExecutionContext, _ControlledTool, _Provider, EventBus]:
    tool = _ControlledTool()
    registry = ToolRegistry()
    registry.register(tool)
    provider = _Provider([
        ToolCallBlock(id=name, name=tool.name, input={"name": name})
        for name in tool.started
    ])
    bus = EventBus()
    loop = AgentLoop(provider, registry, bus, permission_manager=permission_manager, session_id="s1")
    return loop, ExecutionContext(run_id="r1", goal="test", max_steps=3), tool, provider, bus


@asynccontextmanager
async def _running(loop: AgentLoop, context: ExecutionContext) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(loop.run(context))
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _wait(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1.0)


async def test_parallel_tools_start_together_and_return_results_in_request_order() -> None:
    loop, context, tool, provider, bus = _setup()
    second_finished = asyncio.Event()

    async def record(event: BaseModel) -> None:
        if isinstance(event, ToolCallFinishedEvent) and event.tool_use_id == "second":
            second_finished.set()

    bus.subscribe(record)
    async with _running(loop, context) as task:
        for started in tool.started.values():
            await _wait(started)
        tool.release["second"].set()
        await _wait(second_finished)
        assert not provider.observed_results  # The next LLM turn waits for the whole batch.
        assert not task.done()
        tool.release["first"].set()
        await asyncio.wait_for(task, timeout=1.0)

    assert context.status == "success"
    assert [(r["tool_use_id"], r["content"]) for r in provider.observed_results] == [
        ("first", "first"), ("second", "second"),
    ]


async def test_cancelling_parallel_tools_waits_for_all_cleanup() -> None:
    loop, context, tool, _, _ = _setup()
    async with _running(loop, context) as task:
        for started in tool.started.values():
            await _wait(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert tool.cancelled == {"first", "second"}
    assert context.status == "failed"
    assert context.reason == "cancelled"


async def test_cancelling_batch_preserves_already_completed_results() -> None:
    loop, context, tool, _, bus = _setup()
    first_finished = asyncio.Event()

    async def record(event: BaseModel) -> None:
        if isinstance(event, ToolCallFinishedEvent) and event.tool_use_id == "first":
            first_finished.set()

    bus.subscribe(record)
    tool.release["first"].set()
    async with _running(loop, context) as task:
        await _wait(tool.started["second"])
        await _wait(first_finished)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert context.messages[-1] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "first", "content": "first"}],
    }
    assert tool.cancelled == {"second"}


async def test_unexpected_event_error_cancels_other_tools() -> None:
    loop, context, tool, _, bus = _setup()

    async def fail_on_second_start(event: BaseModel) -> None:
        if isinstance(event, ToolCallStartedEvent) and event.tool_use_id == "second":
            await tool.started["first"].wait()
            raise RuntimeError("event subscriber failed")

    bus.subscribe(fail_on_second_start)
    async with _running(loop, context) as task:
        await _wait(tool.started["first"])
        with pytest.raises(ExceptionGroup, match="TaskGroup"):
            await asyncio.wait_for(task, timeout=1.0)

    assert tool.cancelled == {"first"}


async def test_tool_failure_does_not_cancel_successful_sibling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mini_claude.core.tools.invocation._RETRY_BASE_S", 0)
    loop, context, tool, provider, _ = _setup()
    # Missing 'name' raises inside the first tool; the second must still complete.
    provider.calls[0] = ToolCallBlock(id="first", name=tool.name, input={})
    tool.release["second"].set()
    await asyncio.wait_for(loop.run(context), timeout=1.0)
    assert context.status == "success"
    assert provider.observed_results[0]["is_error"] is True
    assert provider.observed_results[1]["content"] == "second"
    assert not tool.cancelled


async def test_cancelled_tool_does_not_send_incomplete_results_to_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, context, tool, provider, _ = _setup()
    invoke = tool.invoke

    async def cancel_first(params: dict[str, object]) -> ToolResult:
        if params["name"] == "first":
            raise asyncio.CancelledError
        return await invoke(params)

    monkeypatch.setattr(tool, "invoke", cancel_first)
    tool.release["second"].set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(loop.run(context), timeout=1.0)

    assert context.reason == "cancelled"
    assert not provider.observed_results
    assert context.messages[-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "second", "content": "second"},
    ]


async def test_parallel_permissions_are_independent_and_denial_does_not_stop_sibling() -> None:
    manager = PermissionManager(timeout_s=0)
    loop, context, tool, provider, bus = _setup(permission_manager=manager)
    requests = {name: asyncio.Event() for name in tool.started}

    async def record(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            requests[event.tool_use_id].set()

    bus.subscribe(record)
    async with _running(loop, context) as task:
        for requested in requests.values():
            await _wait(requested)
        assert not any(event.is_set() for event in tool.started.values())
        manager.respond("second", "allow_once")
        await _wait(tool.started["second"])
        assert not tool.started["first"].is_set()
        tool.release["second"].set()
        manager.respond("first", "deny_once")
        await asyncio.wait_for(task, timeout=1.0)

    assert provider.observed_results[0]["is_error"] is True
    assert provider.observed_results[1]["content"] == "second"
    assert manager.pending_for("s1") == []


async def test_cancel_during_permission_publication_clears_pending_requests() -> None:
    manager = PermissionManager(timeout_s=0)
    loop, context, tool, _, bus = _setup(permission_manager=manager)
    requests = {name: asyncio.Event() for name in tool.started}

    async def block_publication(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            requests[event.tool_use_id].set()
            await asyncio.Event().wait()

    bus.subscribe(block_publication)
    async with _running(loop, context) as task:
        for requested in requests.values():
            await _wait(requested)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert manager.pending_for("s1") == []
    assert not any(event.is_set() for event in tool.started.values())
