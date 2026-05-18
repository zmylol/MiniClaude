from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from mini_claude.core.config import MiniConfig
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.types import LlmResponse, ToolCallBlock
from mini_claude.core.runner import AgentRunner

# --- mock provider -----------------------------------------------------------


class _EndTurnProvider:
    """Immediately returns end_turn; no API calls made."""

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
    ) -> LlmResponse:
        return LlmResponse(stop_reason="end_turn", text="done")


class _LoopingProvider:
    """Always returns tool_use with an unknown tool to exhaust max_steps."""

    def __init__(self) -> None:
        self._call = 0

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
    ) -> LlmResponse:
        self._call += 1
        tc = ToolCallBlock(id=f"t{self._call}", name="unknown_tool", input={})
        return LlmResponse(stop_reason="tool_use", tool_calls=[tc])


# --- helpers -----------------------------------------------------------------


def _config(max_steps: int = 5) -> MiniConfig:
    cfg = MiniConfig()
    cfg.agent.max_steps = max_steps
    return cfg


async def _run(
    goal: str = "test goal",
    *,
    provider: object | None = None,
    config: MiniConfig | None = None,
    tmp_path: Path,
) -> list[BaseModel]:
    collected: list[BaseModel] = []

    async def _collect(e: BaseModel) -> None:
        collected.append(e)

    cfg = config or _config()
    runner = AgentRunner(
        cfg,
        provider=provider or _EndTurnProvider(),  # type: ignore[arg-type]
        extra_handlers=[_collect],
        runs_dir=tmp_path,
    )
    await runner.run(goal)
    return collected


# --- tests -------------------------------------------------------------------


# 功能：验证 run 开始时发布携带正确 goal 的 run.started 事件
# 设计：用 extra_handlers 收集事件，而非从 events.jsonl 读取，避免文件 I/O 耦合；聚焦 runner 层的事件发布职责
async def test_run_started_event_published(tmp_path: Path) -> None:
    events = await _run(goal="my goal", tmp_path=tmp_path)
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert "run.started" in types
    started = next(e for e in events if e.type == "run.started")  # type: ignore[attr-defined]
    assert started.goal == "my goal"  # type: ignore[attr-defined]


# 功能：验证成功完成时发布 status=success 的 run.finished 事件
# 设计：EndTurnProvider 触发最短成功路径，聚焦 runner 层对任何终止路径都能保证发布 finished 事件
async def test_run_finished_event_published_on_success(tmp_path: Path) -> None:
    events = await _run(tmp_path=tmp_path)
    finished = next(
        (e for e in events if e.type == "run.finished"), None  # type: ignore[attr-defined]
    )
    assert finished is not None
    assert finished.status == "success"  # type: ignore[attr-defined]


# 功能：验证步数耗尽时 run.finished 携带 failed 状态和正确的失败原因
# 设计：LoopingProvider + max_steps=2 触发失败路径，确认 runner 在失败终止路径同样发布 finished 事件
async def test_run_finished_event_published_on_max_steps(tmp_path: Path) -> None:
    events = await _run(
        provider=_LoopingProvider(),
        config=_config(max_steps=2),
        tmp_path=tmp_path,
    )
    finished = next(e for e in events if e.type == "run.finished")  # type: ignore[attr-defined]
    assert finished.status == "failed"  # type: ignore[attr-defined]
    assert finished.reason == "exceeded_max_steps"  # type: ignore[attr-defined]


# 功能：验证 events.jsonl 第一行为 run.started、最后一行为 run.finished
# 设计：从 tmp_path 递归查找 events.jsonl 并按行解析，因为 events.jsonl 是 S1 的核心产物，首尾事件是完整性的最低要求
async def test_events_jsonl_created_with_started_and_finished(tmp_path: Path) -> None:
    await _run(tmp_path=tmp_path)
    jsonl_files = list(tmp_path.rglob("events.jsonl"))
    assert len(jsonl_files) == 1
    lines = [json.loads(ln) for ln in jsonl_files[0].read_text().splitlines() if ln]
    event_types = [e["type"] for e in lines]
    assert event_types[0] == "run.started"
    assert event_types[-1] == "run.finished"


# 功能：验证 runner 在 runs_dir 下创建以 run_id 命名的子目录并写入 events.jsonl
# 设计：检查 tmp_path 下只有一个子目录且该目录包含 events.jsonl，确认目录结构约定（runs/<run_id>/events.jsonl）
async def test_run_creates_run_subdirectory(tmp_path: Path) -> None:
    await _run(tmp_path=tmp_path)
    subdirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(subdirs) == 1
    assert (subdirs[0] / "events.jsonl").exists()


# 功能：验证通过 extra_handlers 注入的回调能收到所有事件
# 设计：注入第二个收集器，确认 extra_handlers 机制有效；这是测试代码注入 mock 观察器、生产代码接入 StdoutPrinter 的同一扩展点
async def test_extra_handlers_receive_events(tmp_path: Path) -> None:
    secondary: list[BaseModel] = []

    async def _second(e: BaseModel) -> None:
        secondary.append(e)

    cfg = _config()
    runner = AgentRunner(
        cfg,
        provider=_EndTurnProvider(),  # type: ignore[arg-type]
        extra_handlers=[_second],
        runs_dir=tmp_path,
    )
    await runner.run("goal")
    assert len(secondary) > 0


# 功能：验证 config.agent.max_steps 被正确传递给 AgentLoop，控制 LLM 调用次数上限
# 设计：用 LoopingProvider 的调用次数反推 max_steps 是否生效，不依赖内部状态检查，从行为角度验证配置传递
async def test_config_max_steps_passed_to_loop(tmp_path: Path) -> None:
    provider = _LoopingProvider()
    await _run(provider=provider, config=_config(max_steps=3), tmp_path=tmp_path)
    assert provider._call == 3


# 功能：验证 run.started 和 run.finished 事件使用相同且非空的 run_id
# 设计：同时检查两个事件的 run_id 字段，确认 runner 在整个 run 生命周期使用同一个 run_id
async def test_run_id_embedded_in_started_event(tmp_path: Path) -> None:
    events = await _run(tmp_path=tmp_path)
    started = next(e for e in events if e.type == "run.started")  # type: ignore[attr-defined]
    finished = next(e for e in events if e.type == "run.finished")  # type: ignore[attr-defined]
    assert started.run_id == finished.run_id  # type: ignore[attr-defined]
    assert len(started.run_id) > 0  # type: ignore[attr-defined]


# 功能：验证注入外部 EventBus 时，runner 使用该 bus 而不自建，外部订阅者能收到所有事件
# 设计：显式传入 EventBus 实例并订阅收集器，确认 runner 不再内部新建 bus（否则外部订阅者收不到事件）；
#       这是 CoreApp 注入全局 bus 的核心行为，单元测试级别验证可避免集成测试的守护进程依赖
async def test_injected_bus_receives_events(tmp_path: Path) -> None:
    from mini_claude.core.events.bus import EventBus

    external_bus = EventBus()
    collected: list[object] = []

    async def collect(e: object) -> None:
        collected.append(e)

    external_bus.subscribe(collect)

    runner = AgentRunner(
        _config(),
        bus=external_bus,
        provider=_EndTurnProvider(),  # type: ignore[arg-type]
        runs_dir=tmp_path,
    )
    await runner.run("goal")

    types = [e.type for e in collected]  # type: ignore[attr-defined]
    assert "run.started" in types
    assert "run.finished" in types
