from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mini_claude.core.config import MiniConfig
from mini_claude.desktop import __main__ as cli
from mini_claude.desktop import app
from mini_claude.desktop.core import DEFAULT_CONNECTION_ENV, prepare_config


# 构造真实 JSON-RPC 握手内容的连接替身。
# 测试只使用内存流，不访问已有 core 或用户文件。
def core_socket(project: Path | None) -> MagicMock:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    result = {
        "server_version": "0.0.1", "uptime_ms": 1, "received_at": "2026-09-05T00:00:00Z",
        "project_path": str(project) if project else None,
    }
    response = {"jsonrpc": "2.0", "id": "mini-desktop", "result": result}
    connection.makefile.return_value = io.BytesIO(json.dumps(response).encode() + b"\n")
    return connection


# 功能：已有 core 验证项目后建立连接复用。
# 设计：桌面退出不得启动或结束外部进程。
def test_existing_core_is_never_stopped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    connect = MagicMock(return_value=core_socket(tmp_path))
    spawn = MagicMock()
    monkeypatch.setattr(app.socket, "create_connection", connect)
    monkeypatch.setattr(app.subprocess, "Popen", spawn)
    core = app.CoreProcess(MiniConfig(), tmp_path)

    core.start()
    core.stop()

    connect.return_value.__exit__.assert_called_once()
    spawn.assert_not_called()


# 功能：没有运行中的 core 时自动启动当前项目后端。
# 设计：关闭桌面只结束这个实例创建的进程。
def test_owned_core_starts_in_project_and_is_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    connect = MagicMock(side_effect=[ConnectionRefusedError, core_socket(tmp_path)])
    process = MagicMock()
    process.poll.return_value = None
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(app.socket, "create_connection", connect)
    monkeypatch.setattr(app.subprocess, "Popen", spawn)
    core = app.CoreProcess(MiniConfig(port=8123), tmp_path)

    core.start()
    core.stop()
    core.stop()

    assert spawn.call_args.args[0] == [sys.executable, "-m", "mini_claude.core"]
    assert spawn.call_args.kwargs["cwd"] == tmp_path
    assert spawn.call_args.kwargs["env"]["MINI_PORT"] == "8123"
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)


# 功能：后端启动后立即失败时提供明确异常。
# 设计：失败进程仍需回收且不重复发送结束信号。
def test_core_start_failure_reaps_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process = MagicMock()
    process.poll.return_value = 1
    monkeypatch.setattr(app.socket, "create_connection", MagicMock(side_effect=OSError))
    monkeypatch.setattr(app.subprocess, "Popen", MagicMock(return_value=process))
    core = app.CoreProcess(MiniConfig(), tmp_path)

    with pytest.raises(RuntimeError, match="mini-core.*1"):
        core.start()
    core.stop()

    process.terminate.assert_not_called()
    process.wait.assert_called_once_with(timeout=5)


# 功能：健康检查不能覆盖仍存活的自有进程。
# 设计：模拟存活但暂时失联，检查不会启动重复后端或遗失所有权。
def test_core_reconnect_does_not_replace_an_unresponsive_owned_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    core = app.CoreProcess(MiniConfig(), tmp_path)
    process = MagicMock()
    process.poll.return_value = None
    core.process = process
    spawn = MagicMock()
    monkeypatch.setattr(core, "available", lambda: False)
    monkeypatch.setattr(app.subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError):
        core.start(timeout=0)
    assert core.process is process
    spawn.assert_not_called()


# 功能：后端超时必须退出启动流程并回收子进程。
# 设计：强制结束仅作为当前实例子进程不响应退出时的后备。
def test_core_timeout_terminates_owned_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    process = MagicMock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("core", 5), None]
    monkeypatch.setattr(app.socket, "create_connection", MagicMock(side_effect=OSError))
    monkeypatch.setattr(app.subprocess, "Popen", MagicMock(return_value=process))
    core = app.CoreProcess(MiniConfig(), tmp_path)

    with pytest.raises(RuntimeError, match="超时"):
        core.start(timeout=0)
    core.stop()

    process.terminate.assert_called_once()
    process.kill.assert_called_once()


# 功能：已有 core 所属项目不同或旧版本无法返回项目时拒绝复用。
# 设计：错误路径既不启动新进程，也不结束外部 core。
@pytest.mark.parametrize("legacy", [False, True])
def test_existing_core_project_must_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, legacy: bool,
) -> None:
    connection = core_socket(None if legacy else tmp_path / "other-project")
    spawn = MagicMock()
    monkeypatch.setattr(app.socket, "create_connection", MagicMock(return_value=connection))
    monkeypatch.setattr(app.subprocess, "Popen", spawn)
    core = app.CoreProcess(MiniConfig(), tmp_path)

    with pytest.raises(RuntimeError, match="重启" if legacy else "项目不一致"):
        core.start()
    core.stop()

    spawn.assert_not_called()


# 功能：其他服务占用端口时不把它识别为 mini-core。
# 设计：不完整或不匹配的响应均在发送运行命令前被拒绝。
@pytest.mark.parametrize("response", [b"HTTP/1.1 200 OK\n", b'{"jsonrpc":"2.0","id":"wrong","result":{}}\n'])
def test_existing_service_must_complete_core_handshake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response: bytes,
) -> None:
    connection = core_socket(tmp_path)
    connection.makefile.return_value = io.BytesIO(response)
    spawn = MagicMock()
    monkeypatch.setattr(app.socket, "create_connection", MagicMock(return_value=connection))
    monkeypatch.setattr(app.subprocess, "Popen", spawn)
    core = app.CoreProcess(MiniConfig(), tmp_path)

    with pytest.raises(RuntimeError, match="mini-core.*握手"):
        core.start()
    core.stop()

    spawn.assert_not_called()


# 功能：core.ping 返回实际执行工具所用的项目目录。
# 设计：直接调用真实处理器验证字段来源而不启动后台服务。
async def test_core_ping_reports_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from mini_claude.core.app import CoreApp

    monkeypatch.chdir(tmp_path)
    result = await CoreApp()._ping_handler({"client": "desktop-test"})
    assert result.project_path == str(tmp_path)


# 功能：网关以真实 HTTP 响应提供现有界面和项目元信息。
# 设计：清理后后台线程应退出且不公开配置中的凭据。
def test_gateway_serves_project_and_stops(tmp_path: Path) -> None:
    gateway = app.DesktopGateway(MiniConfig(), tmp_path, port=0)
    try:
        url = gateway.start()
        assert url.startswith("http://127.0.0.1:")
        with urllib.request.urlopen(f"{url}/api/info", timeout=3) as response:
            info = json.load(response)
        assert info["project_path"] == str(tmp_path)
        assert set(info) == {"project_name", "project_path", "project_selected", "model", "core_host", "core_port"}
    finally:
        gateway.stop()
    assert not gateway.thread.is_alive()


# 功能：已占用的桌面端口必须清晰报错。
# 设计：失败网关线程应退出并释放已经创建的资源。
def test_gateway_port_conflict_is_cleaned_up(tmp_path: Path) -> None:
    first = app.DesktopGateway(MiniConfig(), tmp_path, port=0)
    try:
        url = first.start()
        second = app.DesktopGateway(MiniConfig(), tmp_path, port=int(url.rsplit(":", 1)[1]))
        with pytest.raises(RuntimeError, match="端口"):
            second.start()
        second.stop()
        assert not second.thread.is_alive()
    finally:
        first.stop()


# 功能：网关启动超时与端口被占用是不同故障。
# 设计：即使 TimeoutError 继承 OSError，也应保留准确的超时提示。
def test_gateway_timeout_is_reported_as_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    gateway = app.DesktopGateway(MiniConfig(), tmp_path)
    monkeypatch.setattr(gateway.thread, "start", MagicMock())
    monkeypatch.setattr(gateway.ready, "result", MagicMock(side_effect=TimeoutError))
    try:
        with pytest.raises(RuntimeError, match="超时"):
            gateway.start()
    finally:
        gateway.stop()


# 功能：独立窗口通过本机事件网关加载界面。
# 设计：在不同主屏幕下验证初始尺寸、最小尺寸和显示器选择，退出时资源按所有权清理。
@pytest.mark.parametrize(("screen_size", "window_size", "minimum_size"), [
    ((2560, 1440), (1440, 960), (900, 650)),
    ((1440, 900), (1376, 804), (900, 650)),
    ((1366, 768), (1302, 672), (900, 650)),
    ((800, 600), (736, 504), (736, 504)),
    (None, (1440, 960), (900, 650)),
])
@pytest.mark.parametrize("project_selected", [True, False])
def test_desktop_window_lifecycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, screen_size: tuple[int, int] | None,
    window_size: tuple[int, int], minimum_size: tuple[int, int], project_selected: bool,
) -> None:
    webview = MagicMock(screens=[])
    primary = (SimpleNamespace(x=0, y=0, width=screen_size[0], height=screen_size[1])
               if screen_size is not None else None)
    secondary = SimpleNamespace(x=-2560, y=0, width=2560, height=1440)
    webview.screens = [secondary, primary] if primary else []
    core = MagicMock()
    gateway = MagicMock()
    gateway.start.return_value = "http://127.0.0.1:7439"
    monkeypatch.setattr(app, "load_webview", lambda: webview)
    monkeypatch.setattr(app, "install_quit_handler", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(app, "CoreProcess", MagicMock(return_value=core))
    monkeypatch.setattr(app, "DesktopGateway", MagicMock(return_value=gateway))

    app.run_desktop(MiniConfig(), tmp_path, storage_path=tmp_path / "storage",
                    project_selected=project_selected)

    workspace = app.DesktopGateway.call_args.kwargs["workspace"]
    expected_path = tmp_path if project_selected else tmp_path / "storage/workspace"
    assert workspace.project_path == expected_path
    assert workspace.project_selected is True
    assert app.CoreProcess.call_args.args[1] == expected_path
    assert workspace.default_path.is_dir()

    assert webview.create_window.call_args.args == ("MiniClaude",)
    options = webview.create_window.call_args.kwargs
    assert options["url"] == "http://127.0.0.1:7439/?desktop=1"
    assert (options["width"], options["height"]) == window_size
    assert options["min_size"] == minimum_size
    assert options["screen"] is primary
    assert options.get("frameless", False) is False
    assert "js_api" not in options
    assert webview.settings.__setitem__.call_args_list[-1].args == ("ALLOW_DOWNLOADS", True)
    webview.start.assert_called_once_with(private_mode=False, storage_path=str(tmp_path / "storage"))
    core.start.assert_called_once()
    gateway.stop.assert_called_once()
    core.stop.assert_called_once()


# 功能：窗口或网关启动失败不应遗留桌面拥有的后台服务。
# 设计：两种失败路径都在异常返回前清理网关与 core。
@pytest.mark.parametrize("failure", ["gateway", "window", "core"])
def test_desktop_startup_failure_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str,
) -> None:
    webview = MagicMock(screens=[])
    core = MagicMock()
    gateway = MagicMock()
    gateway.start.return_value = "http://127.0.0.1:7439"
    target = {"gateway": gateway.start, "window": webview.create_window, "core": core.start}[failure]
    target.side_effect = RuntimeError("startup failed")
    monkeypatch.setattr(app, "load_webview", lambda: webview)
    monkeypatch.setattr(app, "install_quit_handler", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(app, "CoreProcess", MagicMock(return_value=core))
    monkeypatch.setattr(app, "DesktopGateway", MagicMock(return_value=gateway))

    with pytest.raises(RuntimeError, match="startup failed"):
        app.run_desktop(MiniConfig(), tmp_path, storage_path=tmp_path / "storage")

    gateway.stop.assert_called_once()
    core.stop.assert_called_once()


# 功能：macOS 原生退出通知必须在 Python 主循环返回前释放后台服务。
# 设计：重复通知和随后执行的 finally 共享幂等清理，不重复终止进程。
def test_native_quit_cleans_up_before_webview_returns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    webview = MagicMock(screens=[])
    core = MagicMock()
    gateway = MagicMock()
    gateway.start.return_value = "http://127.0.0.1:7439"
    install = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(app, "load_webview", lambda: webview)
    monkeypatch.setattr(app, "install_quit_handler", install)
    monkeypatch.setattr(app, "CoreProcess", MagicMock(return_value=core))
    monkeypatch.setattr(app, "DesktopGateway", MagicMock(return_value=gateway))

    # 模拟 Cocoa 在 webview.start 尚未返回时广播退出通知。
    # 同步断言确保清理不依赖 finally 才执行。
    def native_terminate(**options: object) -> None:
        callback = install.call_args.args[0]
        callback()
        callback()
        gateway.stop.assert_called_once()
        core.stop.assert_called_once()

    webview.start.side_effect = native_terminate

    app.run_desktop(MiniConfig(), tmp_path, storage_path=tmp_path / "storage")

    gateway.stop.assert_called_once()
    core.stop.assert_called_once()
    install.return_value.assert_called_once()


# 功能：原生通知观察者应正确注册并在普通窗口关闭后注销。
# 设计：用 Foundation 替身验证 Cocoa 桥接，不创建真实应用窗口。
def test_native_quit_observer_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    foundation = MagicMock()
    center = foundation.NSNotificationCenter.defaultCenter.return_value
    cleanup = MagicMock()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(app.importlib, "import_module", MagicMock(return_value=foundation))

    remove = app.install_quit_handler(cleanup)
    arguments = center.addObserverForName_object_queue_usingBlock_.call_args.args
    assert arguments[:3] == ("NSApplicationWillTerminateNotification", None, None)
    arguments[3](object())
    cleanup.assert_called_once()
    remove()

    center.removeObserver_.assert_called_once_with(
        center.addObserverForName_object_queue_usingBlock_.return_value,
    )


# 功能：未安装可选桌面依赖时显示可直接执行的启动指令。
# 设计：缺依赖时不创建后台服务或泄漏完整配置。
def test_missing_desktop_dependency_has_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.importlib, "import_module", MagicMock(side_effect=ImportError))
    with pytest.raises(RuntimeError, match="uv run --extra desktop mini-desktop"):
        app.load_webview()


# 功能：macOS 应用名称应显示为 MiniClaude。
# 设计：使用替身 Foundation 验证原生设置而不影响测试进程。
def test_macos_application_name(monkeypatch: pytest.MonkeyPatch) -> None:
    webview = MagicMock()
    foundation = MagicMock()
    info: dict[str, str] = {}
    foundation.NSBundle.mainBundle.return_value.infoDictionary.return_value = info
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(app.importlib, "import_module", MagicMock(side_effect=[webview, foundation]))

    assert app.load_webview() is webview

    foundation.NSProcessInfo.processInfo.return_value.setProcessName_.assert_called_once_with("MiniClaude")
    assert info["CFBundleName"] == "MiniClaude"


# 功能：从应用启动器选定的项目目录加载配置。
# 设计：桌面存储与监听端口参数原样传入窗口运行器。
def test_cli_loads_config_from_selected_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    run = MagicMock()
    config = MiniConfig()
    config_paths: list[Path] = []

    # 记录读取配置时的工作目录。
    # 返回测试配置避免读取真实用户配置。
    def get_config() -> MiniConfig:
        config_paths.append(Path.cwd())
        monkeypatch.setenv("MINI_TEST_PROJECT_ONLY", "project-value")
        return config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "get_config", get_config)
    monkeypatch.setattr(cli, "run_desktop", run)
    monkeypatch.setattr(sys, "argv", [
        "mini-desktop", "--project", str(project), "--port", "7440",
        "--storage-path", str(tmp_path / "storage"),
    ])

    cli.main()

    assert config_paths == [project]
    assert run.call_count == 1
    assert run.call_args.args == (config, project)
    assert run.call_args.kwargs["port"] == 7440
    assert run.call_args.kwargs["storage_path"] == tmp_path / "storage"
    assert "MINI_TEST_PROJECT_ONLY" not in run.call_args.kwargs["environment"]


# 功能：无效启动参数应在读取配置或启动后端前被拒绝。
# 设计：两种输入均提供标准命令行错误退出码。
@pytest.mark.parametrize("arguments", [["--port", "0"], ["--project", "/no-such-mini-project"]])
def test_cli_rejects_invalid_options(monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    run = MagicMock()
    monkeypatch.setattr(cli, "run_desktop", run)
    monkeypatch.setattr(sys, "argv", ["mini-desktop", *arguments])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    run.assert_not_called()


# 功能：桌面启动异常以清晰的本地提示结束。
# 设计：显式使用临时项目与目录，错误输出不包含配置，失败也不能恢复真实最近项目或污染环境。
def test_cli_reports_startup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    environment = dict(os.environ)
    monkeypatch.setattr(cli, "get_config", lambda: MiniConfig())
    run = MagicMock(side_effect=RuntimeError("窗口启动失败"))
    monkeypatch.setattr(cli, "run_desktop", run)
    monkeypatch.setattr(sys, "argv", [
        "mini-desktop", "--project", str(tmp_path),
        "--storage-path", str(tmp_path / "storage"),
    ])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "MiniClaude 桌面启动失败：窗口启动失败" in capsys.readouterr().err
    assert run.call_args.args[1] == tmp_path
    assert run.call_args.kwargs["storage_path"] == tmp_path / "storage"
    assert dict(os.environ) == environment


# 功能：切换项目启动独立 core 并保留旧项目，关闭时只回收自身拥有的进程。
# 设计：记录子进程的工作目录与传入环境，确保项目 dotenv 不会污染下一项目。
def test_project_cores_keep_running_projects_and_isolate_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    first = MagicMock(config=MiniConfig())
    second = MagicMock()
    initial = tmp_path / "first"
    target = tmp_path / "second"
    target.mkdir()
    baseline = {"PATH": os.environ["PATH"], "MINI_TEST_SYSTEM": "system-value"}
    registry = app.ProjectCores(initial, first, baseline)
    monkeypatch.setenv("MINI_TEST_PROJECT_ONLY", "never-inherit")
    snapshot = MagicMock(return_value=subprocess.CompletedProcess([], 0, b'{"model":"model-b"}', b""))
    factory = MagicMock(return_value=second)
    monkeypatch.setattr(app.subprocess, "run", snapshot)
    monkeypatch.setattr(app, "CoreProcess", factory)

    config = registry.open(target)
    second.config = config
    assert registry.open(target) is config
    assert snapshot.call_args.kwargs["cwd"] == target
    assert snapshot.call_args.kwargs["env"] == baseline
    assert factory.call_args.args[1] == target
    assert factory.call_args.kwargs["environment"] == baseline
    assert config.port != 7437
    assert config.llm.default_model == "model-b"
    assert second.start.call_count == 2
    first.stop.assert_not_called()
    registry.stop()
    first.stop.assert_called_once()
    second.stop.assert_called_once()


# 功能：原生文件夹选择取消时不改变工作区，选定文件夹直接交给切换入口。
# 设计：调用窗口运行器真实安装的选择回调，避免以字符串路径输入假装系统对话框。
def test_desktop_installs_native_folder_picker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    webview = MagicMock(screens=[])
    gateway = MagicMock()
    gateway.start.return_value = "http://127.0.0.1:7439"
    factory = MagicMock(return_value=gateway)
    monkeypatch.setattr(app, "load_webview", lambda: webview)
    monkeypatch.setattr(app, "install_quit_handler", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(app, "CoreProcess", MagicMock())
    monkeypatch.setattr(app, "DesktopGateway", factory)
    app.run_desktop(MiniConfig(), tmp_path, storage_path=tmp_path / "storage")
    workspace = factory.call_args.kwargs["workspace"]
    dialog = webview.create_window.return_value.create_file_dialog
    dialog.return_value = None
    assert workspace.pick_project() is None
    dialog.return_value = [str(tmp_path)]
    assert workspace.pick_project() == str(tmp_path)
    dialog.assert_called_with(webview.FileDialog.FOLDER, directory=str(tmp_path))


# 功能：初始默认端口属于其他项目时自动改用独立端口，避免应用在选择器出现前退出。
# 设计：真实 CoreProcess 保留身份校验，仅以握手替身模拟冲突和第二次启动成功。
def test_initial_project_uses_separate_port_when_another_core_is_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    config = MiniConfig()
    core = app.CoreProcess(config, tmp_path, environment={})
    start = MagicMock(side_effect=[RuntimeError("项目不一致"), None])
    monkeypatch.setattr(core, "start", start)
    registry = app.ProjectCores(tmp_path, core, {})
    assert registry.start_initial(tmp_path) is config
    assert config.port != 7437
    assert start.call_count == 2
    assert core.process is None


# 功能：未显式选择项目时恢复最近目录，显式参数始终具有更高优先级。
# 设计：使用隔离存储运行 CLI 两次，避免读取用户真实最近项目记录。
@pytest.mark.parametrize("explicit", [False, True])
def test_cli_restores_last_project_without_overriding_explicit_choice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit: bool,
) -> None:
    saved = tmp_path / "saved"
    saved.mkdir()
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "projects.json").write_text(json.dumps({"current_path": str(saved)}))
    run = MagicMock()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "get_config", lambda: MiniConfig())
    monkeypatch.setattr(cli, "run_desktop", run)
    monkeypatch.setattr(sys, "argv", [
        "mini-desktop", "--storage-path", str(storage),
        *(["--project", str(tmp_path)] if explicit else []),
    ])
    cli.main()
    assert run.call_args.args[1] == (tmp_path if explicit else saved)


# 功能：移除最后一个项目后普通启动保持空选择，显式项目参数仍可重新打开目录。
# 设计：复用真实入口解析持久空值，验证启动目录不会被意外加回项目列表。
@pytest.mark.parametrize("explicit", [False, True])
def test_cli_preserves_explicit_empty_project_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit: bool,
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "projects.json").write_text(json.dumps({
        "projects": [], "current_path": None, "project_selected": False,
    }))
    run = MagicMock()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "get_config", lambda: MiniConfig())
    monkeypatch.setattr(cli, "run_desktop", run)
    monkeypatch.setattr(sys, "argv", [
        "mini-desktop", "--storage-path", str(storage),
        *(["--project", str(tmp_path)] if explicit else []),
    ])
    cli.main()
    assert run.call_args.kwargs["project_selected"] is explicit


# 功能：普通文件夹复用桌面默认连接，而自带地址或密钥的项目绝不混用默认凭据。
# 设计：在隔离环境中读取真实 dotenv 文件，覆盖无配置、完整配置与部分配置三种项目。
@pytest.mark.parametrize("project_env", ["", "ANTHROPIC_BASE_URL=https://own.invalid", "ANTHROPIC_API_KEY=project-test-key"])
def test_project_connection_fallback_respects_local_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_env: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(project_env)
    environment = {
        "MINI_CONFIG": str(tmp_path / "missing-config.toml"),
        DEFAULT_CONNECTION_ENV: json.dumps({
            "ANTHROPIC_API_KEY": "default-test-key",
            "ANTHROPIC_BASE_URL": "https://default.invalid",
            "MINI_LLM_DEFAULT_MODEL": "default-test-model",
        }),
    }
    monkeypatch.setattr(os, "environ", environment)
    if project_env.startswith("ANTHROPIC_BASE_URL"):
        with pytest.raises(RuntimeError, match="自己的 ANTHROPIC_API_KEY"):
            prepare_config()
        assert "ANTHROPIC_API_KEY" not in environment
        return
    config = prepare_config()
    assert DEFAULT_CONNECTION_ENV not in environment
    if not project_env:
        assert environment["ANTHROPIC_API_KEY"] == "default-test-key"
        assert environment["ANTHROPIC_BASE_URL"] == "https://default.invalid"
        assert config.llm.default_model == "default-test-model"
    else:
        assert environment["ANTHROPIC_API_KEY"] == "project-test-key"
        assert "ANTHROPIC_BASE_URL" not in environment


# 功能：重启到无配置文件夹时恢复最初模型连接来源，最近项目记录不保存任何密钥。
# 设计：隔离子进程读取测试 dotenv，验证父进程环境未污染且来源路径在再次切换后仍保存。
def test_reopen_plain_folder_restores_connection_source_without_persisting_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from mini_claude.web.workspace import Workspace

    source = tmp_path / "configured"
    plain = tmp_path / "plain"
    storage = tmp_path / "storage"
    source.mkdir()
    plain.mkdir()
    storage.mkdir()
    (source / ".env").write_text(
        "ANTHROPIC_API_KEY=source-test-key\nANTHROPIC_BASE_URL=https://source.invalid\n"
        "MINI_LLM_DEFAULT_MODEL=source-test-model\n",
    )
    (storage / "projects.json").write_text(json.dumps({
        "projects": [str(plain), str(source)], "current_path": str(plain),
        "connection_project_path": str(source),
    }))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    settings, restored_source = app.restore_default_connection(
        storage, plain, {"MINI_CONFIG": str(tmp_path / "missing.toml")}, MiniConfig(),
    )
    assert settings["ANTHROPIC_API_KEY"] == "source-test-key"
    assert settings["MINI_LLM_DEFAULT_MODEL"] == "source-test-model"
    assert restored_source == source
    assert "ANTHROPIC_API_KEY" not in os.environ
    Workspace(MiniConfig(), plain, storage_path=storage, connection_project_path=restored_source)
    saved = (storage / "projects.json").read_text()
    assert "source-test-key" not in saved
    assert "https://source.invalid" not in saved
    assert json.loads(saved)["connection_project_path"] == str(source)


# 功能：应用退出与打开项目同时发生时，迟到的 core 会被回收且退出后禁止再次启动。
# 设计：用事件阻塞真实后台线程的启动阶段，关闭先标记状态再等待，释放后检查两个 core 均只清理一次。
def test_shutdown_waits_for_inflight_project_start_and_reaps_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    initial = MagicMock(config=MiniConfig())
    late = MagicMock()
    target = tmp_path / "late"
    target.mkdir()
    registry = app.ProjectCores(tmp_path, initial, {})

    # 功能：暂停子进程启动以精确制造关闭先于登记的竞争。
    # 设计：使用有界事件等待，测试失败时也能释放工作线程而不挂住整个测试进程。
    def start() -> None:
        entered.set()
        if not release.wait(timeout=3):
            raise RuntimeError("test start gate timed out")

    late.start.side_effect = start
    factory = MagicMock(return_value=late)
    monkeypatch.setattr(app, "CoreProcess", factory)
    monkeypatch.setattr(app.subprocess, "run", MagicMock(return_value=
                        subprocess.CompletedProcess([], 0, b'{"model":"test-model"}', b"")))
    with ThreadPoolExecutor(max_workers=2) as pool:
        opening = pool.submit(registry.open, target)
        try:
            assert entered.wait(timeout=2)
            closing = pool.submit(registry.stop)
            assert registry._closed.wait(timeout=2)
            assert not closing.done()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="退出"):
            opening.result(timeout=2)
        closing.result(timeout=2)
    initial.stop.assert_called_once()
    late.stop.assert_called_once()
    assert registry.cores == {}
    with pytest.raises(RuntimeError, match="退出"):
        registry.open(target)
    with pytest.raises(RuntimeError, match="退出"):
        registry.start_initial(tmp_path)
    registry.stop()
    late.stop.assert_called_once()
    factory.assert_called_once()


# 功能：项目连接不会将 shell 的密钥发送给新地址，也不会将项目密钥发送给 shell 的旧地址。
# 设计：使用真实 dotenv 解析四种来源组合，分别验证完整项目覆盖、单独密钥使用默认服务、无项目配置继承及部分配置拒绝。
@pytest.mark.parametrize("local_connection", ["", "ANTHROPIC_BASE_URL=https://project.invalid", "ANTHROPIC_API_KEY=project-key", "ANTHROPIC_API_KEY=project-key\nANTHROPIC_BASE_URL=https://project.invalid"])
def test_project_connection_keeps_inherited_credentials_and_endpoints_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, local_connection: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(local_connection)
    environment = {
        "MINI_CONFIG": str(tmp_path / "missing.toml"),
        "ANTHROPIC_API_KEY": "shell-key",
        "ANTHROPIC_BASE_URL": "https://shell.invalid",
    }
    monkeypatch.setattr(os, "environ", environment)
    if local_connection.startswith("ANTHROPIC_BASE_URL"):
        with pytest.raises(RuntimeError, match="自己的 ANTHROPIC_API_KEY"):
            prepare_config()
        assert environment["ANTHROPIC_API_KEY"] == "shell-key"
        assert environment["ANTHROPIC_BASE_URL"] == "https://shell.invalid"
        return
    prepare_config()
    if not local_connection:
        assert environment["ANTHROPIC_API_KEY"] == "shell-key"
        assert environment["ANTHROPIC_BASE_URL"] == "https://shell.invalid"
    else:
        assert environment["ANTHROPIC_API_KEY"] == "project-key"
        if "BASE_URL" in local_connection:
            assert environment["ANTHROPIC_BASE_URL"] == "https://project.invalid"
        else:
            assert "ANTHROPIC_BASE_URL" not in environment


# 功能：项目连接不完整时，原生切换显示可操作错误且不启动新 core。
# 设计：真实隔离配置子进程解析只有地址的 dotenv，检查其错误经过公共元信息通道返回而非仅留在 stderr。
def test_project_picker_reports_incomplete_connection_before_starting_core(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / ".env").write_text("ANTHROPIC_BASE_URL=https://target.invalid")
    registry = app.ProjectCores(tmp_path, MagicMock(), {
        "MINI_CONFIG": str(tmp_path / "missing.toml"), "ANTHROPIC_API_KEY": "shell-test-key",
    })
    factory = MagicMock()
    monkeypatch.setattr(app, "CoreProcess", factory)
    with pytest.raises(RuntimeError, match="自己的 ANTHROPIC_API_KEY"):
        registry.open(target)
    factory.assert_not_called()


# 功能：项目密钥或地址中的 dotenv 插值不会展开父进程凭据，避免被转发至其他地址。
# 设计：使用原始变量表达式和隔离哨兵环境，检查拒绝发生在任何连接字段修改之前。
@pytest.mark.parametrize("connection", [
    "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}\nANTHROPIC_BASE_URL=https://project.invalid",
    "ANTHROPIC_API_KEY=project-key\nANTHROPIC_BASE_URL=${PROJECT_ENDPOINT}",
    "ALIAS=${ANTHROPIC_API_KEY}\nANTHROPIC_API_KEY=${ALIAS}\nANTHROPIC_BASE_URL=https://project.invalid",
])
def test_provider_group_rejects_dotenv_credential_interpolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, connection: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(connection)
    environment = {
        "MINI_CONFIG": str(tmp_path / "missing.toml"),
        "ANTHROPIC_API_KEY": "shell-key-sentinel",
        "ANTHROPIC_BASE_URL": "https://shell.invalid",
        "PROJECT_ENDPOINT": "https://project.invalid",
    }
    monkeypatch.setattr(os, "environ", environment)
    with pytest.raises(RuntimeError, match="环境变量替换"):
        prepare_config()
    assert environment["ANTHROPIC_API_KEY"] == "shell-key-sentinel"
    assert environment["ANTHROPIC_BASE_URL"] == "https://shell.invalid"


# 功能：初次启动桌面也必须在通用配置加载器展开 dotenv 之前拒绝连接插值。
# 设计：替换配置加载与窗口入口并断言未调用，覆盖父进程先展开后再传默认连接的绕行路径。
def test_initial_desktop_validates_raw_connection_before_loading_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}\nANTHROPIC_BASE_URL=https://project.invalid",
    )
    environment = {
        "ANTHROPIC_API_KEY": "shell-key-sentinel",
        "ANTHROPIC_BASE_URL": "https://shell.invalid",
    }
    monkeypatch.setattr(os, "environ", environment)
    monkeypatch.chdir(tmp_path)
    load = MagicMock()
    run = MagicMock()
    monkeypatch.setattr(cli, "get_config", load)
    monkeypatch.setattr(cli, "run_desktop", run)
    monkeypatch.setattr(sys, "argv", ["mini-desktop", "--project", str(tmp_path)])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    load.assert_not_called()
    run.assert_not_called()
    error = capsys.readouterr().err
    assert "环境变量替换" in error
    assert "shell-key-sentinel" not in error
    assert environment["ANTHROPIC_BASE_URL"] == "https://shell.invalid"
