from __future__ import annotations

from pathlib import Path


# 拒绝绝对路径和父目录遍历，解析符号链接后确认目标仍属于当前工作区
def resolve_workspace_path(path: Path) -> Path:
    if path.is_absolute():
        raise PermissionError(f"absolute path not allowed: {path}")
    if ".." in path.parts:
        raise PermissionError(f"path traversal not allowed: {path}")
    resolved = path.resolve()
    if not resolved.is_relative_to(Path.cwd().resolve()):
        raise PermissionError(f"path outside workspace: {path}")
    return resolved
