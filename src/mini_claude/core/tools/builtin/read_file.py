from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.paths import resolve_workspace_path

_MAX_BYTES = 512 * 1024  # 512 KB


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str


class ReadFileTool(BaseTool):
    retry_on_error = True
    params_model = ReadFileParams
    name = "read_file"
    description = (
        "Read the text content of a file. "
        "Path must be relative to the current working directory. "
        "Files larger than 512 KB are truncated."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (relative to current working directory).",
            }
        },
        "required": ["path"],
    }

    # 读取工作区内文件内容，解析链接并限制相对路径，超过 512KB 时截断
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        path_str = ReadFileParams.model_validate(params).path

        path = resolve_workspace_path(Path(path_str))
        raw = path.read_bytes()  # raises FileNotFoundError if absent
        truncated = len(raw) > _MAX_BYTES
        text = raw[:_MAX_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated]"

        return ToolResult(content=text)
