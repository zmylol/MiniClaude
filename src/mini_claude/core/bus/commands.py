from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator


class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str


class PongResult(BaseModel):
    server_version: str
    uptime_ms: int
    received_at: str  # ISO 8601


# 根据 type 字段决定命令类型的判别联合
Command = Annotated[
    PingCommand,
    Discriminator("type"),
]
