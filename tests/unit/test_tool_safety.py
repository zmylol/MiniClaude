from __future__ import annotations

from pathlib import Path

import pytest

import mini_claude.core.tools.invocation as invocation
from mini_claude.core.bus.events import Event, PermissionRequestedEvent
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.types import ToolCallBlock
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.tools.base import BaseTool
from mini_claude.core.tools.builtin.bash import BashTool
from mini_claude.core.tools.builtin.list_dir import ListDirTool
from mini_claude.core.tools.builtin.read_file import ReadFileTool
from mini_claude.core.tools.builtin.write_file import WriteFileTool
from mini_claude.core.tools.invocation import invoke_tool
from mini_claude.core.tools.registry import ToolRegistry


@pytest.mark.parametrize("decision", ["allow_once", "deny_once"])
# 功能：验证追加后失败的 Bash 命令经审批只执行一次，拒绝时执行零次
# 设计：真实 shell、调用器和审批管理器共同运行，断言文件副作用及完整事件数量和错误输出
async def test_bash_partial_failure_executes_once_after_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(invocation, "_RETRY_BASE_S", 0.0)
    manager = PermissionManager(policy_file=tmp_path / "policy.toml")
    registry = ToolRegistry()
    registry.register(BashTool())
    bus = EventBus()
    events: list[Event] = []

    # 收集事件并模拟用户对唯一工具调用作出审批决定
    async def _respond(event: Event) -> None:
        events.append(event)
        if isinstance(event, PermissionRequestedEvent):
            manager.respond(event.tool_use_id, decision)

    bus.subscribe(_respond)
    result = await invoke_tool(
        registry,
        ToolCallBlock(
            id="append-once", name="bash",
            input={"command": "printf 'one\\n' >> lines.txt; printf 'failure\\n' >&2; exit 7"},
        ),
        bus, "run", permission_manager=manager, session_id="session",
    )
    assert result.is_error
    assert sum(event.type == "permission.requested" for event in events) == 1
    assert sum(event.type == "tool.call_started" for event in events) == 1
    assert sum(event.type == "tool.call_failed" for event in events) == 1
    assert sum(event.type == "tool.call_finished" for event in events) == 0
    if decision == "allow_once":
        assert (tmp_path / "lines.txt").read_text() == "one\n"
        assert result.content == "[exit 7]\nfailure\n"
        assert sum(event.type == "permission.granted" for event in events) == 1
    else:
        assert not (tmp_path / "lines.txt").exists()
        assert result.error_type == "permission_denied"
        assert sum(event.type == "permission.denied" for event in events) == 1
    assert manager.pending_for("session") == []


@pytest.mark.parametrize("tool_class", [ReadFileTool, WriteFileTool, ListDirTool])
@pytest.mark.parametrize("inside_workspace", [False, True])
# 功能：验证文件工具拒绝所有绝对路径，包括工作区内部的绝对路径
# 设计：准备可读取和可写入的真实目标，排除文件不存在造成的假阳性，并检查未发生写入
async def test_file_tools_reject_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tool_class: type[BaseTool], inside_workspace: bool,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    directory = workspace if inside_workspace else tmp_path
    target = directory / "file.txt"
    target.write_text("original")
    path = directory if tool_class is ListDirTool else target
    with pytest.raises(PermissionError):
        await tool_class().invoke({"path": str(path), "content": "changed"})
    assert target.read_text() == "original"


@pytest.mark.parametrize("tool_class", [ReadFileTool, WriteFileTool, ListDirTool])
# 功能：验证相对符号链接不能使文件工具访问工作区外部文件或目录
# 设计：入口是合法相对路径且不含双点，确保测试覆盖链接解析后的真实归属检查
async def test_file_tools_reject_external_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_class: type[BaseTool],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("original")
    (workspace / "escape").symlink_to(outside if tool_class is ListDirTool else target)
    monkeypatch.chdir(workspace)
    with pytest.raises(PermissionError):
        await tool_class().invoke({"path": "escape", "content": "changed"})
    assert target.read_text() == "original"


# 功能：验证新建文件时父目录符号链接不能导致外部目录或文件被创建
# 设计：通过指向外部目录的链接写入尚不存在的两层路径，覆盖 resolve 对不存在尾部的处理
async def test_write_file_rejects_external_parent_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(workspace)
    with pytest.raises(PermissionError):
        await WriteFileTool().invoke({"path": "escape/new/file.txt", "content": "changed"})
    assert list(outside.iterdir()) == []


# 功能：验证列出工作区时遇到外部符号链接会拒绝，而不会递归展示外部内容
# 设计：入口固定为合法的当前目录，嵌套链接只在递归过程中出现，防止仅入口检查漏过越界
async def test_list_dir_rejects_external_link_during_recursion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private")
    (nested / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(workspace)
    with pytest.raises(PermissionError):
        await ListDirTool().invoke({"path": ".", "max_depth": 4})


# 功能：验证工作区内的相对路径和内部符号链接仍支持创建、读取及列目录
# 设计：三个真实工具顺序访问同一目录树，避免边界收紧误伤正常流程及内部链接
async def test_file_tools_allow_relative_paths_and_internal_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    written = await WriteFileTool().invoke({"path": "nested/file.txt", "content": "hello"})
    (tmp_path / "alias").symlink_to(tmp_path / "nested", target_is_directory=True)
    read = await ReadFileTool().invoke({"path": "alias/file.txt"})
    listed = await ListDirTool().invoke({"path": "."})
    assert not written.is_error
    assert read.content == "hello"
    assert "file.txt" in listed.content
