from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field


class WorkspaceListCommand(BaseModel):
    type: Literal["workspace.list"] = "workspace.list"


class WorkspacePickCommand(BaseModel):
    type: Literal["workspace.pick"] = "workspace.pick"


class WorkspaceSelectCommand(BaseModel):
    type: Literal["workspace.select"] = "workspace.select"
    path: str = Field(min_length=1, max_length=4096)


class WorkspaceSessionsCommand(BaseModel):
    type: Literal["workspace.sessions"] = "workspace.sessions"
    path: str = Field(min_length=1, max_length=4096)


class WorkspaceRemoveCommand(BaseModel):
    type: Literal["workspace.remove"] = "workspace.remove"
    path: str = Field(min_length=1, max_length=4096)


class WorkspaceFilesCommand(BaseModel):
    type: Literal["workspace.files"] = "workspace.files"
    path: str = Field(default="", max_length=4096)


class WorkspaceGitStatusCommand(BaseModel):
    type: Literal["workspace.git_status"] = "workspace.git_status"


class WorkspaceGitDiffCommand(BaseModel):
    type: Literal["workspace.git_diff"] = "workspace.git_diff"
    path: str = Field(default="", max_length=4096)


class WorkspacePullRequestsCommand(BaseModel):
    type: Literal["workspace.pull_requests"] = "workspace.pull_requests"


WorkspaceCommand = Annotated[
    WorkspaceListCommand | WorkspacePickCommand | WorkspaceSelectCommand | WorkspaceRemoveCommand
    | WorkspaceFilesCommand | WorkspaceSessionsCommand
    | WorkspaceGitStatusCommand | WorkspaceGitDiffCommand | WorkspacePullRequestsCommand,
    Discriminator("type"),
]


class WorkspaceProject(BaseModel):
    path: str
    name: str
    is_default: bool = False


class WorkspaceListResult(BaseModel):
    projects: list[WorkspaceProject]
    current_path: str | None
    project_selected: bool = True


class WorkspaceInfoResult(BaseModel):
    project_name: str
    project_path: str | None
    project_selected: bool = True
    model: str
    core_host: str
    core_port: int


class WorkspaceRemoveResult(WorkspaceInfoResult, WorkspaceListResult):
    pass


class WorkspacePickCancelledResult(BaseModel):
    cancelled: Literal[True] = True


class WorkspaceFileEntry(BaseModel):
    name: str
    path: str
    kind: Literal["file", "directory"]
    size: int


class WorkspaceFilesResult(BaseModel):
    entries: list[WorkspaceFileEntry]


class WorkspaceGitFile(BaseModel):
    path: str
    status: str


class WorkspaceGitStatusResult(BaseModel):
    available: bool
    branch: str
    files: list[WorkspaceGitFile]
    reason: str | None = None


class WorkspaceGitDiffResult(BaseModel):
    diff: str


class WorkspacePullRequestsResult(BaseModel):
    available: bool
    items: list[dict[str, Any]]
    reason: str | None = None


class WorkspaceChangedEvent(WorkspaceInfoResult):
    type: Literal["workspace.changed"] = "workspace.changed"


class WorkspaceProjectsChangedEvent(WorkspaceListResult):
    type: Literal["workspace.projects_changed"] = "workspace.projects_changed"
