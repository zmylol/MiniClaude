from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.paths import resolve_workspace_path

_MAX_DEPTH = 4
_MAX_ENTRIES = 200


class ListDirParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = "."
    max_depth: int = Field(default=2, ge=1, le=_MAX_DEPTH)


class ListDirTool(BaseTool):
    retry_on_error = True
    params_model = ListDirParams
    name = "list_dir"
    description = (
        "List the contents of a directory as a tree. "
        "Path must be relative to the current working directory. "
        "Hidden entries (starting with .) are included. "
        f"Maximum depth is {_MAX_DEPTH}, maximum total entries is {_MAX_ENTRIES}."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the directory (default '.').",
            },
            "max_depth": {
                "type": "integer",
                "description": f"How many levels deep to recurse (default 2, max {_MAX_DEPTH}).",
            },
        },
        "required": [],
    }

    # 以树状格式列出目录内容，深度和条数有上限
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ListDirParams.model_validate(params)
        path_str = p.path
        max_depth = p.max_depth

        root = resolve_workspace_path(Path(path_str))
        workspace = Path.cwd().resolve()
        if not root.exists():
            raise FileNotFoundError(f"no such directory: {path_str}")
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {path_str}")

        lines: list[str] = [str(Path(path_str)) + "/"]
        count = 0

        # 每层先检查所有条目的真实归属，防止从合法入口递归进入外部链接
        def _walk(directory: Path, depth: int, prefix: str) -> None:
            nonlocal count
            if depth > max_depth or count >= _MAX_ENTRIES:
                return
            entries = list(directory.iterdir())
            for entry in entries:
                resolve_workspace_path(entry.relative_to(workspace))
            entries.sort(key=lambda e: (e.is_file(), e.name))
            for i, entry in enumerate(entries):
                if count >= _MAX_ENTRIES:
                    lines.append(f"{prefix}... (truncated)")
                    return
                connector = "└── " if i == len(entries) - 1 else "├── "
                suffix = "/" if entry.is_dir() else ""
                lines.append(f"{prefix}{connector}{entry.name}{suffix}")
                count += 1
                if entry.is_dir() and depth < max_depth:
                    extension = "    " if i == len(entries) - 1 else "│   "
                    _walk(entry, depth + 1, prefix + extension)

        _walk(root, 1, "")
        return ToolResult(content="\n".join(lines))
