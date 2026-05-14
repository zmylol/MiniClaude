from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from mini_claude.core.bus.events import RunFinishedEvent, RunStartedEvent
from mini_claude.core.events.bus import EventBus
from mini_claude.core.events.writer import EventWriter


async def test_event_writer_writes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    event = RunStartedEvent(run_id="run-1", goal="test goal", ts="2026-05-11T00:00:00Z")

    async with EventWriter(path) as writer:
        await writer.handle(event)

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == "run.started"
    assert data["run_id"] == "run-1"
    assert data["goal"] == "test goal"


async def test_event_writer_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "runs" / "abc123" / "events.jsonl"
    event = RunStartedEvent(run_id="abc123", goal="test", ts="2026-05-11T00:00:00Z")

    async with EventWriter(path) as writer:
        await writer.handle(event)

    assert path.exists()


async def test_event_writer_appends_multiple_events(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"

    async with EventWriter(path) as writer:
        await writer.handle(RunStartedEvent(run_id="r1", goal="g1", ts="2026-05-11T00:00:00Z"))
        finished = RunFinishedEvent(
            run_id="r1", status="success", steps=2, ts="2026-05-11T00:00:01Z"
        )
        await writer.handle(finished)

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "run.started"
    assert json.loads(lines[1])["type"] == "run.finished"


async def test_event_writer_subscribe_via_bus(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    bus = EventBus()
    event = RunStartedEvent(run_id="r1", goal="g", ts="2026-05-11T00:00:00Z")

    async with EventWriter(path) as writer:
        writer.subscribe(bus)
        await bus.publish(event)

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["run_id"] == "r1"


async def test_event_writer_handle_when_not_open_is_noop(tmp_path: Path) -> None:
    # handle() 在未打开时应静默返回，不抛出异常
    writer = EventWriter(tmp_path / "events.jsonl")
    event = RunStartedEvent(run_id="r1", goal="g", ts="2026-05-11T00:00:00Z")
    await writer.handle(event)  # _file is None, should not raise


async def test_event_writer_oserror_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "events.jsonl"
    event = RunStartedEvent(run_id="r1", goal="g", ts="2026-05-11T00:00:00Z")

    with caplog.at_level(logging.ERROR, logger="mini_claude.core.events.writer"):
        async with EventWriter(path) as writer:
            assert writer._file is not None
            writer._file.close()
            # _file 仍非 None 但已关闭，write 会抛 OSError
            await writer.handle(event)

    assert any("failed to write" in r.message for r in caplog.records)
