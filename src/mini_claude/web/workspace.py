from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from mini_claude.core.bus.commands import SessionSummary
from mini_claude.core.bus.workspace_commands import WorkspaceCommand
from mini_claude.core.config import MiniConfig
from mini_claude.core.session.store import SessionStore

WORKSPACE_COMMAND: TypeAdapter[WorkspaceCommand] = TypeAdapter(WorkspaceCommand)
SESSIONS_ROOT = Path("~/.mini/sessions")

class Workspace:
    # 保存项目入口与原生选择器，网关只公开不含凭据的项目摘要。
    def __init__(
        self, config: MiniConfig, project_path: Path, *, storage_path: Path | None = None,
        open_project: Callable[[Path], MiniConfig] | None = None,
        pick_project: Callable[[], str | None] | None = None,
        connection_project_path: Path | None = None,
        project_selected: bool = True,
        default_path: Path | None = None,
    ) -> None:
        self.config = config
        self.project_path = project_path.resolve()
        self.storage_path = storage_path
        self.connection_project_path = (connection_project_path or project_path).resolve()
        self._open_project = open_project
        self.pick_project = pick_project
        self.project_selected = project_selected
        self.default_path = default_path.expanduser().resolve() if default_path else None
        if self.default_path is not None:
            self.default_path.mkdir(parents=True, exist_ok=True)
        self.before_remove: Callable[[Path], Awaitable[None]] | None = None
        self.removing_projects: set[Path] = set()
        self._lock = asyncio.Lock()
        self._configs = {self.project_path: config}
        self._projects = [path for path in self._read_projects() if path != self.default_path]
        if self.project_selected:
            self._remember(self.project_path)

    # 读取最近项目时忽略已经移走的目录，损坏的数据应明确报告。
    def _read_projects(self) -> list[Path]:
        if self.storage_path is None:
            return []
        path = self.storage_path / "projects.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            paths = data["projects"]
            if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
                raise ValueError("Invalid projects")
            return list(dict.fromkeys(
                Path(item).resolve() for item in paths if Path(item).is_dir()
            ))
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("最近项目记录损坏，请检查桌面存储中的 projects.json。") from exc

    # 用原子替换持久化最近项目顺序，避免退出中断留下半个 JSON。
    def _remember(self, path: Path) -> None:
        projects = self._projects if path == self.default_path else [
            path, *(item for item in self._projects if item != path)
        ][:30]
        self._persist(projects, path)
        self._projects = projects

    # 保存显式空选择，移除最后一个项目后重启不能自动重新添加启动目录。
    def _persist(self, projects: list[Path], current_path: Path | None) -> None:
        if self.storage_path is not None:
            self.storage_path.mkdir(parents=True, exist_ok=True)
            target = self.storage_path / "projects.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps({
                "projects": [str(item) for item in projects],
                "current_path": str(current_path) if current_path else None,
                "project_selected": current_path is not None,
                "connection_project_path": str(self.connection_project_path),
            }, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)

    # 返回渲染器需要的项目摘要，始终不序列化完整运行配置。
    def info(self) -> dict[str, Any]:
        return {
            "project_name": ("默认工作区" if self.project_path == self.default_path
                             else self.project_path.name) if self.project_selected else "",
            "project_path": str(self.project_path) if self.project_selected else None,
            "project_selected": self.project_selected,
            "model": self.config.llm.default_model,
            "core_host": self.config.host, "core_port": self.config.port,
        }

    # 最近项目仅表示界面登记关系，不改变目录、会话或项目设置。
    def listing(self) -> dict[str, Any]:
        projects: list[dict[str, Any]] = [{"path": str(path), "name": path.name}
                                          for path in self._projects if path.is_dir()]
        if self.default_path is not None:
            projects.insert(0, {"path": str(self.default_path), "name": "默认工作区",
                                "is_default": True})
        return {"projects": projects,
                "current_path": str(self.project_path) if self.project_selected else None,
                "project_selected": self.project_selected}

    # 所有需要项目的入口统一拒绝空选择，避免继续使用隐藏的后端工作目录。
    def require_project(self) -> None:
        if not self.project_selected:
            raise ValueError("请先选择或打开一个项目。")

    # 调度器只允许访问仍登记的项目，不会重新领取已经移除的计划。
    def has_project(self, path: Path) -> bool:
        path = path.resolve()
        return (path == self.default_path or path in self._projects
                ) and path not in self.removing_projects and path.is_dir()

    # 枚举有效登记项目供启动恢复计划使用，不启动项目 core 或改变界面选择
    def registered_projects(self) -> list[Path]:
        paths = [*self._projects]
        if self.default_path is not None:
            paths.insert(0, self.default_path)
        return [path for path in paths if self.has_project(path)]

    # 只拦截属于桌面工作区的命令，其他 JSON-RPC 继续传给 core。
    def handles(self, method: str) -> bool:
        return method.startswith("workspace.")

    # 在一个串行边界内切换项目，确保失败时当前项目和运行中的 core 保持不变。
    async def _select(self, path: Path) -> dict[str, Any]:
        path = path.expanduser().resolve()
        if not path.is_dir():
            raise ValueError("项目目录已不存在，请重新选择文件夹。")
        async with self._lock:
            if path != self.project_path or not self.project_selected:
                config = await self._config_for(path)
                self._remember(path)
                self.config, self.project_path = config, path
                self.project_selected = True
            return self.info()

    # 移除登记前检查活动任务，成功后选择下一个项目或持久化空选择。
    async def _remove(self, path: Path) -> dict[str, Any]:
        path = path.expanduser().resolve()
        async with self._lock:
            if path == self.default_path:
                raise ValueError("默认工作区始终保留，不能从列表移除。")
            if path not in self._projects:
                raise ValueError("这个项目已不在项目列表中。")
            self.removing_projects.add(path)
            try:
                if path in self._configs:
                    try:
                        active = await self._request_core(self._configs[path], "session.list", {})
                    except ConnectionRefusedError:
                        active = {"sessions": []}
                    if any(item.get("running") for item in active.get("sessions", [])):
                        raise ValueError("项目中有正在运行的任务，请先停止任务再移除。")
                remaining = [item for item in self._projects if item != path and item.is_dir()]
                selected = self.project_selected and self.project_path == path
                next_path = (self.default_path or (remaining[0] if remaining else None)
                             ) if selected else None
                config = await self._config_for(next_path) if next_path else self.config
                if self.before_remove is not None:
                    await self.before_remove(path)
                current = next_path if selected else (
                    self.project_path if self.project_selected else None
                )
                self._persist(remaining, current)
                self._projects = remaining
                if selected:
                    self.project_selected = next_path is not None
                    if next_path is not None:
                        self.project_path, self.config = next_path, config
                return {**self.info(), **self.listing()}
            finally:
                self.removing_projects.discard(path)

    # 为已选择的项目复用或启动独立 core，使后台任务不因切换项目被结束。
    async def _config_for(self, path: Path) -> MiniConfig:
        if self._open_project is not None:
            self._configs[path] = await asyncio.to_thread(self._open_project, path)
        elif path not in self._configs:
            raise RuntimeError("项目切换需要在 MiniClaude 桌面应用中打开。")
        return self._configs[path]

    # 重连时确认当前 Core 仍可用，并在同一个锁内返回配对的执行目录与连接配置。
    async def prepare_connection(self) -> tuple[Path, MiniConfig]:
        async with self._lock:
            if self.project_selected:
                self.config = await self._config_for(self.project_path)
            return self.project_path, self.config

    # 通过短连接发送桌面服务命令，响应 ID 匹配后关闭而不占用界面订阅。
    async def request_core(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_project()
        return await self.request_core_for(self.project_path, method, params)

    # 计划任务可访问已登记项目的后端，不随界面当前项目变化而串线。
    async def request_core_for(
        self, project_path: Path, method: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        project_path = project_path.resolve()
        async with self._lock:
            if not self.has_project(project_path):
                raise ValueError("请先在桌面中打开这个项目。")
            config = await self._config_for(project_path)
        return await self._request_core(config, method, params)

    # 已运行项目读取实时摘要，未启动项目仅读元数据，浏览侧栏不启动模型或插件。
    async def _sessions(self, path: Path) -> dict[str, Any]:
        path = path.expanduser().resolve()
        async with self._lock:
            if not self.has_project(path):
                raise ValueError("请先在桌面中打开这个项目。")
            config = self._configs.get(path)
        if config is not None:
            try:
                return await self._request_core(config, "session.list", {})
            except ConnectionRefusedError:
                pass
        sessions = await asyncio.to_thread(self._saved_sessions, path)
        return {"project_path": str(path), "sessions": sessions}

    # 只枚举明确属于目标目录的会话，不读取消息正文或收留无归属的旧记录。
    def _saved_sessions(self, path: Path) -> list[dict[str, Any]]:
        root = SESSIONS_ROOT.expanduser()
        if not root.is_dir():
            return []
        return [SessionSummary(session_id=session.id, **session.to_dict()).model_dump()
                for session in SessionStore(root).list_sessions()
                if session.project_path == str(path)]

    # 用固定后端配置完成一次请求，移除检查无需再次获取工作区锁。
    async def _request_core(
        self, config: MiniConfig, method: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(config.host, config.port, limit=64 * 1024 * 1024), 3,
        )
        request_id = f"desktop-{uuid.uuid4().hex}"
        try:
            writer.write(json.dumps({
                "jsonrpc": "2.0", "id": request_id, "method": method,
                "params": {"type": method, **params},
            }).encode() + b"\n")
            await writer.drain()
            async with asyncio.timeout(None if method == "session.send_message" else 60):
                while line := await reader.readline():
                    response = json.loads(line)
                    if response.get("id") != request_id:
                        continue
                    if "error" in response:
                        raise RuntimeError(response["error"].get("message", "后端操作失败"))
                    result = response.get("result")
                    if not isinstance(result, dict):
                        raise RuntimeError("后端返回了无效响应。")
                    return result
            raise RuntimeError("后端连接已关闭，请重新打开项目。")
        finally:
            writer.close()
            await writer.wait_closed()

    # 将目录和文件引用限定在当前项目内部，拒绝绝对路径及逃逸软链接。
    def _path(self, value: object = "") -> Path:
        if not isinstance(value, str) or Path(value).is_absolute():
            raise ValueError("请选择项目内的相对路径。")
        path = (self.project_path / value).resolve()
        if not path.is_relative_to(self.project_path):
            raise ValueError("不能访问当前项目之外的文件。")
        return path

    # 枚举项目文件夹供界面选择引用，不读取文件正文或隐藏凭据内容。
    def _files(self, params: dict[str, Any]) -> dict[str, Any]:
        directory = self._path(params.get("path", ""))
        if not directory.is_dir():
            raise ValueError("文件夹不存在。")
        entries = []
        for path in directory.iterdir():
            if path.name in {".git", "node_modules", ".venv", "__pycache__"}:
                continue
            if not path.resolve().is_relative_to(self.project_path):
                continue
            try:
                entries.append({
                    "name": path.name, "path": str(path.relative_to(self.project_path)),
                    "kind": "directory" if path.is_dir() else "file", "size": path.stat().st_size,
                })
            except FileNotFoundError:
                continue
        entries.sort(key=lambda item: (item["kind"] != "directory", item["name"]))
        return {"entries": entries}

    # 用无 shell 的只读子进程读取 Git 数据，并限制输出体积与运行时长。
    async def _command(
        self, arguments: list[str], *, timeout: float = 15, allowed_codes: tuple[int, ...] = (0,),
    ) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments, cwd=self.project_path, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GH_PROMPT_DISABLED": "1"},
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"未安装 {arguments[0]}，请安装后重试。") from exc

        # 持续读取至固定上限，避免超大 diff 在内存中无限累积。
        async def read_limited(stream: asyncio.StreamReader | None) -> bytes:
            assert stream is not None
            chunks = bytearray()
            while chunk := await stream.read(65536):
                chunks.extend(chunk)
                if len(chunks) > 2 * 1024 * 1024:
                    raise RuntimeError("输出超过 2 MB，请选择单个文件查看。")
            return bytes(chunks)

        readers = [asyncio.create_task(read_limited(stream))
                   for stream in (process.stdout, process.stderr)]
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await asyncio.gather(*readers)
                await process.wait()
            if process.returncode not in allowed_codes:
                detail = stderr.decode("utf-8", errors="replace").strip()[:1200]
                raise RuntimeError(detail or f"{arguments[0]} 操作失败。")
            return stdout.decode("utf-8", errors="replace")
        except TimeoutError as exc:
            raise RuntimeError("操作超时，请检查网络或仓库状态后重试。") from exc
        finally:
            for task in readers:
                task.cancel()
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            await asyncio.gather(*readers, return_exceptions=True)

    # 解析 NUL 分隔的状态，正确保留带空格、换行和重命名的文件名。
    async def _git_status(self) -> dict[str, Any]:
        try:
            output = await self._command([
                "git", "status", "--porcelain=v1", "-z", "--branch", "--untracked-files=all",
                "--", ".",
            ])
            prefix = (await self._command([
                "git", "rev-parse", "--show-prefix",
            ])).removesuffix("\n")
        except RuntimeError as exc:
            return {"available": False, "branch": "", "files": [], "reason": str(exc)}
        records = output.split("\0")
        branch = ""
        if records and records[0].startswith("## "):
            branch = records.pop(0)[3:].split("...")[0]
        files = []
        while records:
            record = records.pop(0)
            if not record:
                continue
            status, path = record[:2], record[3:]
            if prefix and path.startswith(prefix):
                path = path[len(prefix):]
            files.append({"path": path, "status": status})
            if "R" in status or "C" in status:
                records.pop(0)
        return {"available": True, "branch": branch, "files": files}

    # 从 GitHub CLI 读取开放 PR，未认证或无远端时给出可操作的真实原因。
    async def _pull_requests(self) -> dict[str, Any]:
        try:
            output = await self._command([
                "gh", "pr", "list", "--limit", "50", "--json",
                "number,title,state,url,headRefName,baseRefName,isDraft,updatedAt,author",
            ])
            return {"available": True, "items": json.loads(output)}
        except (RuntimeError, ValueError) as exc:
            return {"available": False, "items": [], "reason": str(exc)}

    # 将工作区操作映射到真实项目、系统选择器和只读仓库能力。
    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        params = WORKSPACE_COMMAND.validate_python({**params, "type": method}).model_dump()
        if method == "workspace.list":
            return self.listing()
        if method == "workspace.sessions":
            return await self._sessions(Path(params["path"]))
        if method == "workspace.remove":
            return await self._remove(Path(params["path"]))
        if method == "workspace.pick":
            if self.pick_project is None:
                raise RuntimeError("原生文件夹选择器需要在 MiniClaude 桌面应用中使用。")
            selected = await asyncio.to_thread(self.pick_project)
            return await self._select(Path(selected)) if selected else {"cancelled": True}
        if method == "workspace.select":
            value = params.get("path")
            if not isinstance(value, str) or not self.has_project(Path(value)):
                raise ValueError("请选择最近项目，或使用「打开文件夹」添加项目。")
            return await self._select(Path(value))
        async with self._lock:
            self.require_project()
            return await self._inspect(method, params)

    # 文件与多步骤 Git 查询期间固定项目，防止其他窗口切换导致结果跨目录混合。
    async def _inspect(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "workspace.files":
            return await asyncio.to_thread(self._files, params)
        if method == "workspace.git_status":
            return await self._git_status()
        if method == "workspace.git_diff":
            path = params.get("path", "")
            self._path(path)
            if path:
                status = await self._git_status()
                if any(item["path"] == path and item["status"] == "??"
                       for item in status["files"]):
                    return {"diff": await self._command([
                        "git", "diff", "--no-index", "--no-ext-diff", "--no-textconv", "--",
                        os.devnull, path,
                    ], allowed_codes=(0, 1))}
            arguments = ["git", "--literal-pathspecs", "diff", "--relative", "--no-ext-diff",
                         "--no-textconv", "HEAD", "--", path or "."]
            try:
                return {"diff": await self._command(arguments)}
            except RuntimeError as exc:
                if "bad revision 'HEAD'" not in str(exc):
                    raise
                staged = await self._command([
                    "git", "--literal-pathspecs", "diff", "--relative", "--cached",
                    "--no-ext-diff", "--no-textconv", "--", path or ".",
                ])
                unstaged = await self._command([
                    "git", "--literal-pathspecs", "diff", "--relative",
                    "--no-ext-diff", "--no-textconv", "--", path or ".",
                ])
                return {"diff": staged + unstaged}
        if method == "workspace.pull_requests":
            return await self._pull_requests()
        raise ValueError(f"不支持的工作区操作：{method}")
