from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mini_claude.core.config import MiniConfig
from mini_claude.core.session.model import Session
from mini_claude.core.session.store import SessionStore
from mini_claude.web.workspace import Workspace


# 功能：默认工作区有真实目录和独立后端，可切换且不会重复登记或被移除。
# 设计：独立端口标识两个后端，验证默认目录不依赖任何 Git 项目。
async def test_default_workspace_is_permanent_and_uses_its_own_core(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    default = tmp_path / "storage/workspace"
    opener = MagicMock(return_value=MiniConfig(port=8123))
    workspace = Workspace(MiniConfig(), project, storage_path=default.parent,
                          default_path=default, open_project=opener)
    assert default.is_dir()
    assert workspace.listing()["projects"][0] == {
        "path": str(default), "name": "默认工作区", "is_default": True,
    }
    result = await workspace.handle("workspace.select", {"path": str(default)})
    assert result["project_path"] == str(default)
    assert result["project_name"] == "默认工作区"
    assert result["core_port"] == 8123
    workspace._request_core = AsyncMock(return_value={"session_id": "sess-default"})
    await workspace.request_core("session.create", {"title": "随手聊聊"})
    assert workspace._request_core.call_args.args[0].port == 8123
    with pytest.raises(ValueError, match="默认工作区"):
        await workspace.handle("workspace.remove", {"path": str(default)})
    assert len(workspace.listing()["projects"]) == 2
    record = json.loads((default.parent / "projects.json").read_text())
    assert record["projects"] == [str(project)]
    assert record["current_path"] == str(default)


# 功能：移除当前项目回到可用默认工作区，重建实例后仍保留默认选择和项目历史。
# 设计：模拟真实持久化与独立 Core 响应，排除只修改界面名称的实现。
async def test_remove_current_returns_to_default_and_survives_restart(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    history = project / "history.json"
    history.write_text("keep")
    default = tmp_path / "storage/workspace"
    workspace = Workspace(MiniConfig(), project, storage_path=default.parent,
                          default_path=default, open_project=lambda _: MiniConfig(port=8123))
    workspace._request_core = AsyncMock(return_value={"sessions": []})
    result = await workspace.handle("workspace.remove", {"path": str(project)})
    assert result["project_path"] == str(default)
    assert result["project_selected"] is True
    restored = Workspace(MiniConfig(port=8123), default, storage_path=default.parent,
                         default_path=default)
    restored.require_project()
    assert restored.listing()["projects"] == [{
        "path": str(default), "name": "默认工作区", "is_default": True,
    }]
    assert history.read_text() == "keep"


# 功能：展开其他项目仅查询该项目的会话摘要，不改变当前执行目录；未登记路径不能查询。
# 设计：检查请求使用的后端配置与项目选择，覆盖侧栏跨项目读取的隔离边界。
async def test_workspace_sessions_are_scoped_without_changing_selection(tmp_path: Path) -> None:
    default = tmp_path / "storage/workspace"
    workspace = Workspace(MiniConfig(), tmp_path, default_path=default,
                          open_project=lambda _: MiniConfig(port=8123))
    workspace._configs[default] = MiniConfig(port=8123)
    workspace._request_core = AsyncMock(return_value={"sessions": [{
        "session_id": "sess-default", "title": "默认聊天", "project_path": str(default),
    }]})
    result = await workspace.handle("workspace.sessions", {"path": str(default)})
    assert result["sessions"][0]["session_id"] == "sess-default"
    assert workspace.project_path == tmp_path
    assert workspace._request_core.call_args.args[0].port == 8123
    assert workspace._request_core.call_args.args[1:] == ("session.list", {})
    with pytest.raises(ValueError, match="打开"):
        await workspace.handle("workspace.sessions", {"path": str(tmp_path / "unknown")})


# 功能：查看未启动项目的聊天目录只读取已保存摘要，不启动后端或混入其他项目。
# 设计：临时存储包含两个项目的元数据，启动器设为失败以发现隐式启动。
async def test_inactive_workspace_history_does_not_start_a_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mini_claude.web.workspace.SESSIONS_ROOT", tmp_path / "sessions", raising=False)
    default = tmp_path / "desktop/workspace"
    opener = MagicMock(side_effect=RuntimeError("must not start"))
    workspace = Workspace(MiniConfig(), tmp_path, default_path=default, open_project=opener)
    store = SessionStore(tmp_path / "sessions")
    for sid, path in [("sess-default", default), ("sess-project", tmp_path)]:
        store.write_meta(Session(id=sid, title="同名聊天", mode="chat", status="waiting_for_input",
                                 project_path=str(path), created_at="2026-09-11", updated_at="2026-09-11"))
    result = await workspace.handle("workspace.sessions", {"path": str(default)})
    assert [item["session_id"] for item in result["sessions"]] == ["sess-default"]
    opener.assert_not_called()
    assert workspace.project_path == tmp_path


# 功能：已登记后台 Core 退出后，实际操作通过启动器重新取得可用连接。
# 设计：先放入旧配置，再令启动器返回新端口，验证请求没有绕过恢复逻辑。
async def test_background_workspace_request_refreshes_cached_core(tmp_path: Path) -> None:
    restored = MiniConfig(port=8123)
    opener = MagicMock(return_value=restored)
    workspace = Workspace(MiniConfig(), tmp_path, open_project=opener)
    workspace._request_core = AsyncMock(return_value={"session_id": "sess-restored"})
    await workspace.request_core_for(tmp_path, "session.create", {"title": "恢复后台任务"})
    opener.assert_called_once_with(tmp_path)
    assert workspace._request_core.call_args.args[0] is restored


# 功能：浏览已退出后台项目时仍能读取磁盘历史，不为浏览操作启动 Core。
# 设计：明确模拟连接被拒绝，并验证只读摘要回退及启动器未被调用。
async def test_inactive_core_refused_connection_still_lists_saved_history(tmp_path: Path) -> None:
    opener = MagicMock()
    workspace = Workspace(MiniConfig(), tmp_path, open_project=opener)
    workspace._request_core = AsyncMock(side_effect=ConnectionRefusedError)
    workspace._saved_sessions = MagicMock(return_value=[{"session_id": "sess-saved"}])
    result = await workspace.handle("workspace.sessions", {"path": str(tmp_path)})
    assert result == {"project_path": str(tmp_path), "sessions": [{"session_id": "sess-saved"}]}
    opener.assert_not_called()


# 功能：原生选择取消无副作用，选定项目持久化并可在重启后从最近列表切换。
# 设计：用独立配置和临时目录确认返回结果、当前路径与持久记录一致。
async def test_picker_cancel_switch_and_recent_projects(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    opener = MagicMock(return_value=MiniConfig(port=8123))
    picker = MagicMock(side_effect=[None, str(second)])
    workspace = Workspace(MiniConfig(), first, storage_path=tmp_path / "storage",
                          open_project=opener, pick_project=picker)
    assert await workspace.handle("workspace.pick", {}) == {"cancelled": True}
    opener.assert_not_called()
    assert workspace.project_path == first
    result = await workspace.handle("workspace.pick", {})
    assert result["project_path"] == str(second)
    assert result["core_port"] == 8123
    assert workspace.project_path == second
    opener.assert_called_once_with(second)
    records = json.loads((tmp_path / "storage/projects.json").read_text())
    assert records["current_path"] == str(second)
    restored = Workspace(MiniConfig(), second, storage_path=tmp_path / "storage", open_project=opener)
    listed = await restored.handle("workspace.list", {})
    assert {project["path"] for project in listed["projects"]} == {str(first), str(second)}
    await restored.handle("workspace.select", {"path": str(first)})
    assert restored.project_path == first


# 功能：项目后端启动失败或未登记路径不能改变当前工作区。
# 设计：失败发生在配置切换与最近记录提交之前，原项目仍可继续使用。
async def test_switch_failure_preserves_current_project(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    workspace = Workspace(MiniConfig(), tmp_path,
                          open_project=MagicMock(side_effect=RuntimeError("launch failed")),
                          pick_project=lambda: str(target))
    with pytest.raises(ValueError, match="最近项目"):
        await workspace.handle("workspace.select", {"path": str(target)})
    with pytest.raises(RuntimeError, match="launch failed"):
        await workspace.handle("workspace.pick", {})
    assert workspace.project_path == tmp_path
    assert workspace.config.port == 7437


# 功能：文件浏览器列出可用目录，拒绝父目录、绝对路径和软链接逃逸。
# 设计：真实临时文件及软链接覆盖容易被字符串前缀检查遗漏的边界。
async def test_files_are_confined_to_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "read me.txt").write_text("hello")
    (project / "outside").symlink_to(tmp_path, target_is_directory=True)
    workspace = Workspace(MiniConfig(), project)
    result = await workspace.handle("workspace.files", {})
    assert [entry["name"] for entry in result["entries"]] == ["src", "read me.txt"]
    assert result["entries"][1]["size"] == 5
    for path in ("..", str(tmp_path), "outside"):
        with pytest.raises(ValueError):
            await workspace.handle("workspace.files", {"path": path})


# 功能：Git 状态与 diff 对包含空格的真实文件提供可查看数据。
# 设计：隔离临时仓库并使用命令行局部作者信息，不接触用户仓库或全局 Git 配置。
async def test_real_git_status_and_diff(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    source = tmp_path / "read me.txt"
    source.write_text("before\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--", source.name], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "--quiet", "-m", "fixture"], check=True)
    source.write_text("after\n")
    workspace = Workspace(MiniConfig(), tmp_path)
    status = await workspace.handle("workspace.git_status", {})
    assert status["available"]
    assert status["files"] == [{"path": "read me.txt", "status": " M"}]
    diff = await workspace.handle("workspace.git_diff", {"path": source.name})
    assert "-before" in diff["diff"]
    assert "+after" in diff["diff"]


# 功能：GitHub CLI 缺失或未认证时显示真实原因，不伪造空 PR 列表成功状态。
# 设计：注入明确错误并检查 UI 所需可用性和原因字段。
async def test_pull_request_unavailability_is_actionable(tmp_path: Path) -> None:
    workspace = Workspace(MiniConfig(), tmp_path)
    workspace._command = AsyncMock(side_effect=RuntimeError("请先登录 gh"))
    assert await workspace.handle("workspace.pull_requests", {}) == {
        "available": False, "items": [], "reason": "请先登录 gh",
    }


# 功能：子目录项目只列出自身变更，未跟踪新文件也能查看添加内容的 diff。
# 设计：父仓库同时包含外部文件与项目文件，检查仓库根路径不会混入当前工作区。
async def test_nested_project_and_untracked_diff(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    project = tmp_path / "nested"
    project.mkdir()
    (tmp_path / "outside.txt").write_text("outside\n")
    (project / "new.txt").write_text("new project content\n")
    workspace = Workspace(MiniConfig(), project)
    status = await workspace.handle("workspace.git_status", {})
    assert status["files"] == [
        {"path": "new.txt", "status": "??"},
    ]
    diff = await workspace.handle("workspace.git_diff", {"path": "new.txt"})
    assert "+new project content" in diff["diff"]


# 功能：桌面服务命令根据目标项目连接正确 core，事件帧不会被误认为响应。
# 设计：真实 TCP 假 core 检查方法及 ID，再发送一个无关事件和匹配结果。
async def test_background_rpc_matches_response_and_target_project(tmp_path: Path) -> None:
    received: list[dict[str, object]] = []

    # 功能：模拟 core 的事件与命令响应混合流。
    # 设计：使用请求 ID 生成回复，避免测试依赖固定内部随机标识。
    async def fake_core(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        received.append(request)
        writer.write(b'{"kind":"event","event":{"type":"run.started"}}\n')
        writer.write(json.dumps({"id": request["id"], "result": {"session_id": "sess-test"}}).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(fake_core, "127.0.0.1", 0) as core:
        workspace = Workspace(MiniConfig(port=core.sockets[0].getsockname()[1]), tmp_path)
        result = await workspace.request_core_for(tmp_path, "session.create", {"title": "scheduled"})
    assert result == {"session_id": "sess-test"}
    assert received[0]["method"] == "session.create"
    assert received[0]["params"] == {"type": "session.create", "title": "scheduled"}


# 功能：仓库查询超时会清理已经派生的后代进程，即使直接子进程提前退出也不会遗漏。
# 设计：父进程退出后子进程继续持有输出管道并写心跳，验证超时后的文件不再变化。
async def test_repository_command_timeout_kills_descendants(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat"
    child = f"import pathlib,time; p=pathlib.Path({str(heartbeat)!r})\nwhile True:\n p.open('a').write('x'); time.sleep(.01)"
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    workspace = Workspace(MiniConfig(), tmp_path)
    with pytest.raises(RuntimeError, match="超时"):
        await workspace._command([sys.executable, "-c", parent], timeout=0.3)
    assert heartbeat.exists()
    snapshot = heartbeat.read_bytes()
    await asyncio.sleep(0.1)
    assert heartbeat.read_bytes() == snapshot


# 功能：文件查询的多步操作完成前，另一个连接不能改变当前项目。
# 设计：在 Git 查询中间挂起并并发选择另一个目录，确认整个查询看到同一项目。
async def test_project_switch_waits_for_repository_inspection(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    entered = asyncio.Event()
    release = asyncio.Event()
    workspace = Workspace(MiniConfig(), tmp_path, open_project=lambda _: MiniConfig(port=8123),
                          pick_project=lambda: str(target))

    # 功能：模拟需要跨两次异步读取的仓库查询。
    # 设计：记录查询开始和结束时的项目，揭示全局路径切换引起的混合结果。
    async def inspect() -> dict[str, object]:
        start = workspace.project_path
        entered.set()
        await release.wait()
        return {"start": start, "end": workspace.project_path}

    workspace._git_status = inspect
    query = asyncio.create_task(workspace.handle("workspace.git_status", {}))
    await entered.wait()
    switching = asyncio.create_task(workspace.handle("workspace.pick", {}))
    await asyncio.sleep(0)
    assert workspace.project_path == tmp_path
    release.set()
    result = await query
    await switching
    assert result == {"start": tmp_path, "end": tmp_path}
    assert workspace.project_path == target


# 功能：移除非当前、当前及最后一个项目时保持正确选择，且目录与会话记录原样保留。
# 设计：参数化真实临时目录并检查持久记录，覆盖列表操作与磁盘数据的边界。
@pytest.mark.parametrize("choice", ["inactive", "current", "last"])
async def test_remove_project_preserves_data_and_selection(tmp_path: Path, choice: str) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    history = first / "conversation.json"
    history.write_text('{"messages":["keep me"]}')
    storage = tmp_path / "storage"
    workspace = Workspace(MiniConfig(), first, storage_path=storage,
                          open_project=lambda _: MiniConfig(port=8123))
    workspace._request_core = AsyncMock(return_value={"sessions": []})
    if choice != "last":
        await workspace._select(second)
    removed = first if choice != "current" else second
    result = await workspace.handle("workspace.remove", {"path": str(removed)})
    expected = str(second) if choice == "inactive" else str(first) if choice == "current" else None
    assert result["current_path"] == expected
    assert result["project_path"] == expected
    assert result["project_selected"] is (choice != "last")
    assert str(removed) not in [item["path"] for item in result["projects"]]
    assert first.is_dir() and second.is_dir()
    assert history.read_text() == '{"messages":["keep me"]}'
    record = json.loads((storage / "projects.json").read_text())
    assert record["current_path"] == expected
    assert record["project_selected"] is (choice != "last")


# 功能：空项目选择可跨重启保留，重新选择同一底层目录会重新登记并恢复使用。
# 设计：重建工作区实例并通过原生选择回调重新添加目录，覆盖路径相等的特殊分支。
async def test_empty_selection_restores_and_same_folder_can_be_readded(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    workspace = Workspace(MiniConfig(), tmp_path, storage_path=storage)
    workspace._request_core = AsyncMock(return_value={"sessions": []})
    await workspace.handle("workspace.remove", {"path": str(tmp_path)})
    restored = Workspace(MiniConfig(), tmp_path, storage_path=storage, project_selected=False,
                         pick_project=lambda: str(tmp_path))
    assert restored.listing() == {"projects": [], "current_path": None, "project_selected": False}
    with pytest.raises(ValueError, match="先选择"):
        await restored.handle("workspace.files", {})
    with pytest.raises(ValueError, match="先选择"):
        await restored.request_core("session.create", {})
    assert (await restored.handle("workspace.pick", {}))["project_path"] == str(tmp_path)
    assert restored.listing()["projects"] == [{"path": str(tmp_path), "name": tmp_path.name}]
    assert json.loads((storage / "projects.json").read_text())["project_selected"] is True


# 功能：正在运行的会话与项目切换失败都会阻止移除，并保持原始项目记录。
# 设计：分别注入后端忙碌和替代项目启动错误，检查列表和选中状态未部分提交。
@pytest.mark.parametrize("failure", ["running", "switch"])
async def test_remove_failure_preserves_project_registration(tmp_path: Path, failure: str) -> None:
    other = tmp_path / "other"
    other.mkdir()
    workspace = Workspace(MiniConfig(), tmp_path, storage_path=tmp_path / "storage",
                          open_project=MagicMock(side_effect=RuntimeError("launch failed")))
    workspace._request_core = AsyncMock(return_value={"sessions": [{"running": failure == "running"}]})
    workspace._projects.append(other)
    before = workspace.listing()
    with pytest.raises((RuntimeError, ValueError), match="运行|launch failed"):
        await workspace.handle("workspace.remove", {"path": str(tmp_path)})
    assert workspace.listing() == before
    assert not workspace.removing_projects
