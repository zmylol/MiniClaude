from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ScheduleFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=20000)
    next_run: AwareDatetime
    repeat: Literal["once", "daily"] = "once"
    enabled: bool = True


class Schedule(ScheduleFields):
    id: str
    status: Literal[
        "scheduled", "submitting", "running", "success", "submitted", "error", "interrupted",
    ] = "scheduled"
    last_run: AwareDatetime | None = None
    last_session_id: str | None = None
    last_error: str | None = None


class SchedulesListCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["schedules.list"] = "schedules.list"


class ScheduleCreateCommand(ScheduleFields):
    type: Literal["schedules.create"] = "schedules.create"


class ScheduleUpdateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["schedules.update"] = "schedules.update"
    id: str
    title: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=20000)
    next_run: AwareDatetime | None = None
    repeat: Literal["once", "daily"] | None = None
    enabled: bool | None = None


class ScheduleDeleteCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["schedules.delete"] = "schedules.delete"
    id: str


class ScheduleRunNowCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["schedules.run_now"] = "schedules.run_now"
    id: str


class SchedulesListResult(BaseModel):
    schedules: list[Schedule]
    runs_only_while_open: bool = True


class ScheduleResult(BaseModel):
    schedule: Schedule
    runs_only_while_open: bool = True


class ScheduleDeleteResult(BaseModel):
    deleted: bool
    id: str


class ScheduleChangedEvent(BaseModel):
    type: Literal["schedules.changed"] = "schedules.changed"
    project_path: str
    schedule: Schedule | None = None
    deleted: bool = False
    id: str | None = None
    runs_only_while_open: bool = True
