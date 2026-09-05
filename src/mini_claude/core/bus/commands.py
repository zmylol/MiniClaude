from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field, model_validator

from mini_claude.core.bus.desktop_commands import (
    PluginsAddCommand,
    PluginsListCommand,
    PluginsRemoveCommand,
    PluginsSetEnabledCommand,
)
from mini_claude.core.bus.schedule_commands import (
    ScheduleCreateCommand,
    ScheduleDeleteCommand,
    ScheduleRunNowCommand,
    SchedulesListCommand,
    ScheduleUpdateCommand,
)
from mini_claude.core.bus.workspace_commands import (
    WorkspaceFilesCommand,
    WorkspaceGitDiffCommand,
    WorkspaceGitStatusCommand,
    WorkspaceListCommand,
    WorkspacePickCommand,
    WorkspacePullRequestsCommand,
    WorkspaceRemoveCommand,
    WorkspaceSelectCommand,
)
from mini_claude.core.session.model import PermissionMode, SessionMode, SessionStatus

ModelName = Annotated[
    str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$"),
]


class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str


class PongResult(BaseModel):
    server_version: str
    uptime_ms: int
    received_at: str  # ISO 8601
    project_path: str | None = None


class AgentRunCommand(BaseModel):
    type: Literal["agent.run"] = "agent.run"
    goal: str


class AgentRunResult(BaseModel):
    run_id: str


class EventSubscribeCommand(BaseModel):
    type: Literal["event.subscribe"] = "event.subscribe"
    topics: list[str]          # fnmatch 模式，如 ["step.*", "tool.*"]
    scope: str = "global"      # "global" | "run:<run_id>"
    replay_from_run: str | None = None  # 设置则先从 events.jsonl 回放历史再接实时流


class EventSubscribeResult(BaseModel):
    subscription_id: str
    replayed_count: int = 0


class SessionCreateCommand(BaseModel):
    type: Literal["session.create"] = "session.create"
    mode: SessionMode = "chat"
    title: str = ""
    model: ModelName | None = None
    permission_mode: PermissionMode = "ask"


class SessionCreateResult(BaseModel):
    session_id: str
    status: SessionStatus
    model: str = ""
    permission_mode: PermissionMode = "ask"


class ImageAttachment(BaseModel):
    name: str = Field(max_length=255)
    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    data: str = Field(max_length=7_000_000)

    # 校验附件大小及图片签名，拒绝伪装成图片的任意二进制数据
    @model_validator(mode="after")
    def validate_image(self) -> ImageAttachment:
        try:
            decoded = base64.b64decode(self.data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid image base64") from exc
        if not decoded or len(decoded) > 5 * 1024 * 1024:
            raise ValueError("image must be between 1 byte and 5 MB")
        signatures = {
            "image/png": decoded.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": decoded.startswith(b"\xff\xd8\xff"),
            "image/gif": decoded.startswith((b"GIF87a", b"GIF89a")),
            "image/webp": decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP",
        }
        if not signatures[self.media_type]:
            raise ValueError("image content does not match media type")
        return self


class SessionSendMessageCommand(BaseModel):
    type: Literal["session.send_message"] = "session.send_message"
    session_id: str
    content: str
    attachments: list[ImageAttachment] = Field(default_factory=list, max_length=5)

    # 限制图片附件总量，避免超大消息耗尽本地桥接器内存
    @model_validator(mode="after")
    def validate_attachment_total(self) -> SessionSendMessageCommand:
        if not self.content.strip() and not self.attachments:
            raise ValueError("message cannot be empty")
        if sum(len(base64.b64decode(item.data)) for item in self.attachments) > 10 * 1024 * 1024:
            raise ValueError("combined images exceed 10 MB")
        return self


class SessionSendMessageResult(BaseModel):
    run_id: str
    cancelled: bool = False
    status: str | None = None
    reason: str | None = None


class PendingPermission(BaseModel):
    tool_use_id: str
    tool_name: str
    params: dict[str, Any]
    param_preview: str
    run_id: str | None = None


class SessionSummary(BaseModel):
    session_id: str
    title: str
    status: SessionStatus
    mode: SessionMode
    model: str
    permission_mode: PermissionMode
    project_path: str
    created_at: str
    updated_at: str
    run_ids: list[str]
    running: bool = False
    active_run_id: str | None = None
    pending_permissions: list[PendingPermission] = Field(default_factory=list)


class SessionListCommand(BaseModel):
    type: Literal["session.list"] = "session.list"


class SessionListResult(BaseModel):
    sessions: list[SessionSummary]
    project_path: str


class SessionRenameCommand(BaseModel):
    type: Literal["session.rename"] = "session.rename"
    session_id: str
    title: str = Field(min_length=1, max_length=200)


class SessionConfigureCommand(BaseModel):
    type: Literal["session.configure"] = "session.configure"
    session_id: str
    model: ModelName | None = None
    permission_mode: PermissionMode | None = None


class SessionUpdateResult(BaseModel):
    session: SessionSummary


class SessionDeleteCommand(BaseModel):
    type: Literal["session.delete"] = "session.delete"
    session_id: str


class SessionDeleteResult(BaseModel):
    deleted: bool = True


class SessionCancelCommand(BaseModel):
    type: Literal["session.cancel"] = "session.cancel"
    session_id: str


class SessionCancelResult(BaseModel):
    cancelled: bool


class ConfigModelsCommand(BaseModel):
    type: Literal["config.models"] = "config.models"


class ModelOption(BaseModel):
    id: str
    label: str


class ConfigModelsResult(BaseModel):
    current_model: str
    models: list[ModelOption]
    allow_custom: bool = True


class SessionGetHistoryCommand(BaseModel):
    type: Literal["session.get_history"] = "session.get_history"
    session_id: str


class SessionGetHistoryResult(BaseModel):
    messages: list[dict[str, Any]]


class SessionCloseCommand(BaseModel):
    type: Literal["session.close"] = "session.close"
    session_id: str


class SessionCloseResult(BaseModel):
    status: SessionStatus


class PermissionRespondCommand(BaseModel):
    type: Literal["permission.respond"] = "permission.respond"
    tool_use_id: str
    # "allow_once" | "always_allow" | "deny_once" | "always_deny"
    decision: str


class PermissionRespondResult(BaseModel):
    ok: bool = True


class SessionCompactCommand(BaseModel):
    type: Literal["session.compact"] = "session.compact"
    session_id: str
    focus: str = ""


class SessionCompactResult(BaseModel):
    summary_tokens: int
    saved_tokens: int


# 根据 type 字段决定命令类型的判别联合
Command = Annotated[
    PingCommand
    | AgentRunCommand
    | EventSubscribeCommand
    | SessionCreateCommand
    | SessionSendMessageCommand
    | SessionGetHistoryCommand
    | SessionCloseCommand
    | PermissionRespondCommand
    | SessionCompactCommand
    | SessionListCommand
    | SessionRenameCommand
    | SessionConfigureCommand
    | SessionDeleteCommand
    | SessionCancelCommand
    | ConfigModelsCommand
    | PluginsAddCommand
    | PluginsListCommand
    | PluginsRemoveCommand
    | PluginsSetEnabledCommand
    | WorkspaceFilesCommand
    | WorkspaceGitDiffCommand
    | WorkspaceGitStatusCommand
    | WorkspaceListCommand
    | WorkspacePickCommand
    | WorkspacePullRequestsCommand
    | WorkspaceRemoveCommand
    | WorkspaceSelectCommand
    | ScheduleCreateCommand
    | ScheduleDeleteCommand
    | ScheduleRunNowCommand
    | SchedulesListCommand
    | ScheduleUpdateCommand,
    Discriminator("type"),
]
