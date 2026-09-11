from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from types import ModuleType

from aiohttp import web

from mini_claude.core.bus.commands import PongResult
from mini_claude.core.bus.envelope import JsonRpcRequest, JsonRpcSuccess
from mini_claude.core.config import LlmConfig, MiniConfig
from mini_claude.desktop.core import DEFAULT_CONNECTION_ENV
from mini_claude.web.server import create_app
from mini_claude.web.workspace import Workspace

logger = logging.getLogger(__name__)


# 恢复普通文件夹时仅根据已保存的来源路径读取默认连接，凭据始终停留在进程内存。
def restore_default_connection(
    storage_path: Path, project_path: Path, environment: dict[str, str], config: MiniConfig,
) -> tuple[dict[str, str], Path]:
    settings = {key: os.environ[key] for key in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
    ) if key in os.environ}
    settings["MINI_LLM_DEFAULT_MODEL"] = config.llm.default_model
    recent = storage_path / "projects.json"
    if any(key in settings for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")):
        return settings, project_path
    if not recent.exists():
        return settings, project_path
    try:
        saved = json.loads(recent.read_text(encoding="utf-8")).get("connection_project_path")
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("最近项目记录损坏，请检查桌面存储中的 projects.json。") from exc
    if not isinstance(saved, str) or not Path(saved).is_dir() or Path(saved) == project_path:
        return settings, project_path
    source = Path(saved).resolve()
    try:
        snapshot = subprocess.run(
            [sys.executable, "-c",
             "import json; from mini_claude.desktop.core import connection_settings; "
             "print(json.dumps(connection_settings()))"],
            cwd=source, env=environment, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("读取应用默认模型连接超时。") from exc
    if snapshot.returncode:
        raise RuntimeError("应用默认模型连接无法读取，请检查原项目的配置。")
    restored = json.loads(snapshot.stdout)
    if not isinstance(restored, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in restored.items()
    ):
        raise RuntimeError("应用默认模型连接格式无效。")
    return restored, source


class CoreProcess:
    # 记录桌面拥有的子进程，已有的外部 core 始终不纳入退出清理。
    def __init__(
        self, config: MiniConfig, project_path: Path, *, environment: dict[str, str] | None = None,
    ) -> None:
        self.config = config
        self.project_path = project_path
        self.process: subprocess.Popen[bytes] | None = None
        self.environment = dict(os.environ) if environment is None else environment.copy()

    # 使用只读握手验证 core 身份与项目路径，避免复用其他项目的执行后端。
    def available(self) -> bool:
        try:
            connection = socket.create_connection((self.config.host, self.config.port), timeout=0.3)
        except OSError:
            return False
        request = JsonRpcRequest(
            id="mini-desktop", method="core.ping", params={"client": "mini-desktop"},
        )
        try:
            with connection:
                connection.settimeout(3)
                connection.sendall(request.model_dump_json().encode() + b"\n")
                with connection.makefile("rb") as stream:
                    line = stream.readline(64 * 1024)
                response = JsonRpcSuccess.model_validate_json(line)
                if response.id != request.id or not line.endswith(b"\n"):
                    raise ValueError("Unexpected core.ping response")
                pong = PongResult.model_validate(response.result)
        except (OSError, ValueError) as exc:
            raise RuntimeError("mini-core 握手失败，请检查配置端口是否被其他服务占用。") from exc
        if not pong.project_path:
            raise RuntimeError("当前 mini-core 未返回项目路径，请更新并重启 core 后再打开桌面。")
        if Path(pong.project_path).resolve() != self.project_path.resolve():
            raise RuntimeError(
                f"mini-core 与桌面项目不一致：core 位于 {pong.project_path}；"
                f"桌面选择 {self.project_path}。请使用同一项目，或为该项目配置独立 core 端口。"
            )
        return True

    # 复用已有 core，否则启动项目后端并等待其监听端口就绪。
    def start(self, timeout: float = 15) -> None:
        if self.process is not None and self.process.poll() is not None:
            self.stop()
        if self.available():
            return
        if self.process is not None:
            raise RuntimeError("mini-core 进程仍在运行但暂时无法连接，请稍后重试。")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "mini_claude.desktop.core"
             if DEFAULT_CONNECTION_ENV in self.environment else "mini_claude.core"],
            cwd=self.project_path,
            env={**self.environment, "MINI_HOST": self.config.host,
                 "MINI_PORT": str(self.config.port)},
            stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = self.process.poll()
            if code is not None:
                raise RuntimeError(f"mini-core 启动失败（退出码 {code}），请检查后端日志。")
            if self.available():
                return
            time.sleep(0.1)
        raise RuntimeError("mini-core 启动超时，请检查后端日志与端口配置。")

    # 仅结束当前桌面启动的子进程，并回收已退出的进程。
    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class ProjectCores:
    # 项目切换只改变当前连接，保留已启动项目的后台任务直至桌面退出。
    def __init__(
        self, project_path: Path, core: CoreProcess, environment: dict[str, str],
        *, default_connection: dict[str, str] | None = None,
    ) -> None:
        self.cores = {project_path.resolve(): core}
        self._lifecycle_lock = threading.RLock()
        self._closed = threading.Event()
        self.environment = environment.copy()
        if default_connection:
            self.environment[DEFAULT_CONNECTION_ENV] = json.dumps(default_connection)

    # 默认端口已属于其他项目时分配独立端口，不接管或终止已有服务。
    def start_initial(self, project_path: Path) -> MiniConfig:
        with self._lifecycle_lock:
            self._ensure_open()
            result = self._start_initial(project_path)
            self._ensure_open()
            return result

    # 在生命周期锁内启动初始项目，结束前不会与退出清理并发操作进程。
    def _start_initial(self, project_path: Path) -> MiniConfig:
        core = self.cores[project_path.resolve()]
        try:
            core.start()
        except RuntimeError:
            if core.process is not None:
                raise
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            core.config.host, core.config.port = "127.0.0.1", port
            core.start()
        return core.config

    # 在隔离进程中读取项目模型，避免上个项目的 dotenv 污染新项目配置。
    def open(self, project_path: Path) -> MiniConfig:
        with self._lifecycle_lock:
            self._ensure_open()
            result = self._open(project_path)
            self._ensure_open()
            return result

    # 应用开始退出后拒绝新项目和迟到的启动结果，让退出方统一回收已登记进程。
    def _ensure_open(self) -> None:
        if self._closed.is_set():
            raise RuntimeError("桌面应用正在退出，项目打开操作已取消。")

    # 在生命周期锁内启动并登记项目，确保清理不会漏掉尚未结束的启动线程。
    def _open(self, project_path: Path) -> MiniConfig:
        project_path = project_path.resolve()
        if project_path in self.cores:
            return self._start_initial(project_path)
        try:
            snapshot = subprocess.run(
                [sys.executable, "-c",
                 "import json\nfrom mini_claude.desktop.core import prepare_config\n"
                 "try:\n config = prepare_config()\n"
                 "except RuntimeError as exc:\n print(json.dumps({'error': str(exc)}))\n"
                 "else:\n print(json.dumps({'model': config.llm.default_model}))"],
                cwd=project_path, env=self.environment, stdin=subprocess.DEVNULL,
                capture_output=True, timeout=10, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("读取项目配置超时。") from exc
        if snapshot.returncode:
            raise RuntimeError("项目配置无法读取，请检查该项目的 .mini/config.toml。")
        try:
            metadata = json.loads(snapshot.stdout)
            if "error" in metadata:
                raise RuntimeError(metadata["error"])
            model = metadata["model"]
        except (ValueError, KeyError) as exc:
            raise RuntimeError("项目配置返回了无效模型信息。") from exc
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = MiniConfig(host="127.0.0.1", port=port, llm=LlmConfig(default_model=model))
        core = CoreProcess(config, project_path, environment=self.environment)
        try:
            core.start()
        except Exception:
            core.stop()
            raise
        self.cores[project_path] = core
        return config

    # 应用退出时回收所有自身启动的项目 core，外部复用进程继续运行。
    def stop(self) -> None:
        self._closed.set()
        with self._lifecycle_lock:
            failure: Exception | None = None
            for core in self.cores.values():
                try:
                    core.stop()
                except Exception as exc:
                    logger.exception("项目后端退出失败")
                    failure = exc
            self.cores.clear()
            if failure is not None:
                raise failure


class DesktopGateway:
    # 用固定本机端口维持存储来源，测试可传入零端口避免冲突。
    def __init__(
        self, config: MiniConfig, project_path: Path, port: int = 7439,
        *, workspace: Workspace | None = None,
    ) -> None:
        self.config = config
        self.project_path = project_path
        self.port = port
        self.workspace = workspace
        self.loop = asyncio.new_event_loop()
        self.shutdown = asyncio.Event()
        self.ready: Future[str] = Future()
        self.runner: web.AppRunner | None = None
        self.thread = threading.Thread(target=self._run, name="mini-desktop-gateway", daemon=True)

    # 创建本机 HTTP 和 WebSocket 网关，等待监听成功后才打开窗口。
    def start(self) -> str:
        self.thread.start()
        try:
            return self.ready.result(timeout=15)
        except TimeoutError as exc:
            raise RuntimeError("桌面事件网关启动超时。") from exc
        except OSError as exc:
            raise RuntimeError(
                f"桌面网关无法监听端口 {self.port}，应用可能已经启动；可用 --port 更换端口。"
            ) from exc

    # 在专用线程中运行异步网关，并在退出和启动失败时释放服务。
    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        except Exception as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
            else:
                raise
        finally:
            try:
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            finally:
                self.loop.close()

    # 通过关闭事件结束服务，避免直接停止事件循环打断资源清理。
    async def _serve(self) -> None:
        try:
            url = await asyncio.wait_for(self._listen(), timeout=10)
            self.ready.set_result(url)
            await self.shutdown.wait()
        finally:
            if self.runner is not None:
                await self.runner.cleanup()

    # 监听回环地址并取得实际绑定端口，复用现有事件协议与静态界面。
    async def _listen(self) -> str:
        self.runner = web.AppRunner(
            create_app(self.config, self.project_path, workspace=self.workspace),
            shutdown_timeout=5,
        )
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        address = self.runner.addresses[0]
        return f"http://127.0.0.1:{address[1]}"

    # 等待网关清理完成，未启动的线程只需关闭尚未使用的事件循环。
    def stop(self) -> None:
        if self.thread.ident is None:
            self.loop.close()
            return
        if self.thread.is_alive() and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.shutdown.set)
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("桌面事件网关未能及时退出。")


# 延迟加载可选桌面依赖，让现有 CLI、TUI 和测试保持轻量。
def load_webview() -> ModuleType:
    try:
        webview = importlib.import_module("webview")
        if sys.platform == "darwin":
            foundation = importlib.import_module("Foundation")
            foundation.NSProcessInfo.processInfo().setProcessName_("MiniClaude")
            info = foundation.NSBundle.mainBundle().infoDictionary()
            info["CFBundleName"] = "MiniClaude"
            info["CFBundleDisplayName"] = "MiniClaude"
        return webview
    except ImportError as exc:
        raise RuntimeError(
            "缺少桌面依赖，请在项目目录执行：uv run --extra desktop mini-desktop"
        ) from exc


# 在 Cocoa 直接结束进程前同步清理服务，返回注销函数供普通窗口关闭使用。
def install_quit_handler(cleanup: Callable[[], None]) -> Callable[[], None]:
    if sys.platform != "darwin":
        return lambda: None
    foundation = importlib.import_module("Foundation")
    center = foundation.NSNotificationCenter.defaultCenter()

    # 原生通知边界记录退出异常，避免 Python 异常跨越 Objective-C 回调。
    def on_terminate(notification: object) -> None:
        try:
            cleanup()
        except Exception:
            logger.exception("MiniClaude 原生退出时清理后台服务失败")

    observer = center.addObserverForName_object_queue_usingBlock_(
        "NSApplicationWillTerminateNotification", None, None, on_terminate,
    )
    return lambda: center.removeObserver_(observer)


# 主线程运行原生窗口，始终按所有权回收本次创建的后台服务。
def run_desktop(
    config: MiniConfig, project_path: Path, *, storage_path: Path | None = None, port: int = 7439,
    environment: dict[str, str] | None = None,
    project_selected: bool = True,
) -> None:
    webview = load_webview()
    storage_path = (storage_path or Path.home() / ".mini" / "desktop").expanduser().resolve()
    storage_path.mkdir(parents=True, exist_ok=True)
    base_environment = dict(os.environ) if environment is None else environment
    default_connection, connection_source = restore_default_connection(
        storage_path, project_path, base_environment, config,
    )
    if (
        config.llm.default_model == MiniConfig().llm.default_model
        and "MINI_LLM_DEFAULT_MODEL" not in os.environ
    ):
        config.llm.default_model = default_connection["MINI_LLM_DEFAULT_MODEL"]
    default_path = storage_path / "workspace"
    default_path.mkdir(parents=True, exist_ok=True)
    if not project_selected:
        project_path = default_path
    core = CoreProcess(config, project_path, environment={
        **base_environment, DEFAULT_CONNECTION_ENV: json.dumps(default_connection),
    })
    projects = ProjectCores(
        project_path, core, base_environment, default_connection=default_connection,
    )
    workspace = Workspace(
        config, project_path, storage_path=storage_path, open_project=projects.open,
        connection_project_path=connection_source,
        default_path=default_path,
    )
    gateway = DesktopGateway(config, project_path, port=port, workspace=workspace)
    cleaned = False

    # 原生退出通知与 Python finally 共用一次清理，不重复结束子进程。
    def cleanup() -> None:
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        try:
            gateway.stop()
        finally:
            projects.stop()

    remove_quit_handler: Callable[[], None] | None = None
    try:
        remove_quit_handler = install_quit_handler(cleanup)
        projects.start_initial(project_path)
        url = gateway.start()
        webview.settings["ALLOW_FILE_URLS"] = False
        webview.settings["ALLOW_DOWNLOADS"] = True
        screens = webview.screens
        screen = next((item for item in screens if item.x == 0 and item.y == 0),
                      screens[0] if screens else None)
        # 使用逻辑像素为窗口边框和系统栏留白，较小屏幕同步降低最小尺寸。
        width = min(1440, screen.width - 64) if screen is not None else 1440
        height = min(960, screen.height - 96) if screen is not None else 960
        window = webview.create_window(
            "MiniClaude", url=f"{url}/?desktop=1", width=width, height=height, screen=screen,
            min_size=(min(900, width), min(650, height)), text_select=True,
            background_color="#FFFFFF",
        )

        # 系统文件夹选择器直接返回用户选择，取消操作不改变项目或启动后端。
        def pick_project() -> str | None:
            selected = window.create_file_dialog(
                webview.FileDialog.FOLDER, directory=str(workspace.project_path),
            )
            return str(selected[0]) if selected else None

        workspace.pick_project = pick_project
        options: dict[str, object] = {"private_mode": False, "storage_path": str(storage_path)}
        icon = project_path / "dist/MiniClaude.app/Contents/Resources/MiniClaude.icns"
        if sys.platform == "darwin" and icon.is_file():
            options["icon"] = str(icon)
        webview.start(**options)
    finally:
        try:
            if remove_quit_handler is not None:
                remove_quit_handler()
        finally:
            cleanup()
