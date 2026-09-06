from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PluginInfo(BaseModel):
    name: str
    transport: Literal["stdio", "tcp"]
    status: Literal["connected", "disabled", "error"]
    tools: list[str]
    managed: bool


class PluginsListCommand(BaseModel):
    type: Literal["plugins.list"] = "plugins.list"


class PluginsAddCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["plugins.add"] = "plugins.add"
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    transport: Literal["stdio", "tcp"]
    command: str = ""
    args: list[str] = Field(default_factory=list, max_length=128)
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=3000, ge=1, le=65535)

    # stdio 插件必须显式指定可执行命令，避免生成无法连接的配置。
    @model_validator(mode="after")
    def validate_transport(self) -> Self:
        if self.transport == "stdio" and not self.command.strip():
            raise ValueError("stdio 插件必须填写启动命令")
        return self


class PluginsSetEnabledCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["plugins.set_enabled"] = "plugins.set_enabled"
    name: str
    enabled: bool


class PluginsRemoveCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["plugins.remove"] = "plugins.remove"
    name: str


class PluginsResult(BaseModel):
    servers: list[PluginInfo]
