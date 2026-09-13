from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mini_claude.core.agents.loader import AgentProfileLoader
from mini_claude.core.config import MiniConfig, _apply_env, _apply_toml
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.types import LlmResponse
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.permissions.policy import PermissionDecision, evaluate
from mini_claude.core.runner import AgentRunner
from mini_claude.core.subagent.registry import BackgroundTaskRegistry
from mini_claude.core.subagent.tool import SpawnAgentTool
from mini_claude.core.task.manager import TaskManager


# 功能：默认注册搜索和抓取，显式白名单仍限制联网工具
# 设计：通过真实 runner 工具注册表检查模型实际可调用的工具，不触发网络
def test_root_network_tools_respect_whitelist(tmp_path: Path) -> None:
    runner = AgentRunner(MiniConfig(), runs_dir=tmp_path)
    manager = TaskManager(tmp_path / "tasks")
    registry = runner._build_registry(manager)
    assert registry.get("web_search") is not None
    assert registry.get("web_fetch") is not None
    limited = runner._build_registry(manager, tool_whitelist=["web_fetch"])
    assert limited.get("web_fetch") is not None
    assert limited.get("web_search") is None
    assert limited.get("read_file") is None


# 功能：网络总开关关闭后不向模型注册联网工具
# 设计：使用实际配置和注册流程确认关闭是执行层行为
def test_network_can_be_disabled(tmp_path: Path) -> None:
    config = MiniConfig()
    _apply_toml(config, {"network": {"enabled": False}})
    registry = AgentRunner(config)._build_registry(TaskManager(tmp_path / "tasks"))
    assert registry.get("web_search") is None
    assert registry.get("web_fetch") is None


# 功能：网络配置支持 TOML 和环境变量覆盖
# 设计：直接应用配置层，避免读取用户真实配置和密钥
def test_network_configuration_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    config = MiniConfig()
    _apply_toml(config, {"network": {
        "enabled": False, "browser_enabled": True, "browser_headless": True,
        "browser_executable_path": "/tmp/test-browser",
    }})
    monkeypatch.setenv("MINI_NETWORK_ENABLED", "true")
    monkeypatch.setenv("MINI_BROWSER_ENABLED", "false")
    monkeypatch.setenv("MINI_BROWSER_HEADLESS", "false")
    monkeypatch.setenv("MINI_BROWSER_EXECUTABLE_PATH", "/tmp/other-browser")
    _apply_env(config)
    assert config.network.enabled is True
    assert config.network.browser_enabled is False
    assert config.network.browser_headless is False
    assert config.network.browser_executable_path == "/tmp/other-browser"


@pytest.mark.parametrize("value", ["yes", 1, [], {"unknown": True}])
# 功能：无效网络配置不会被静默接受
# 设计：覆盖小节类型、布尔类型及未知字段错误
def test_network_configuration_rejects_invalid_values(value: object) -> None:
    with pytest.raises(SystemExit):
        _apply_toml(MiniConfig(), {"network": value})


# 功能：无效网络开关环境变量报告配置错误
# 设计：避免拼写错误导致意外开启网络
def test_network_environment_rejects_invalid_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINI_NETWORK_ENABLED", "tru")
    with pytest.raises(SystemExit, match="MINI_NETWORK_ENABLED"):
        _apply_env(MiniConfig())


@pytest.mark.parametrize("name", ["web_search", "web_fetch"])
# 功能：公开搜索和抓取在默认模式和只读模式可用
# 设计：同时走静态策略与实际权限管理器，避免遗漏只读白名单
async def test_public_network_reads_allowed(name: str) -> None:
    assert evaluate(name, {}) == PermissionDecision.ALLOW
    manager = PermissionManager()
    manager.set_session_mode("session", "read_only")
    emitter = AsyncMock()
    allowed, _ = await manager.check_and_wait("t", name, {}, "session", emitter)
    assert allowed
    emitter.assert_not_called()
    assert evaluate("browser_click", {}) == PermissionDecision.ASK


# 创建每次实例化均可追踪关闭状态的浏览器替身
def _browser_factory(instances: list[AsyncMock]):
    # 构造不启动进程的浏览器替身
    def create(**kwargs: object) -> AsyncMock:
        browser = AsyncMock()
        browser.get_tools = lambda: []
        instances.append(browser)
        return browser
    return create


@pytest.mark.parametrize("failure", [False, True])
# 功能：主任务成功或模型失败都会释放浏览器
# 设计：替换浏览器边界，走完整 runner 并验证 finally 清理
async def test_root_closes_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool,
) -> None:
    instances: list[AsyncMock] = []
    monkeypatch.setattr(
        "mini_claude.core.runner.BrowserSession", _browser_factory(instances), raising=False,
    )
    provider = AsyncMock()
    provider.chat.return_value = LlmResponse(stop_reason="end_turn", text="done")
    if failure:
        provider.chat.side_effect = RuntimeError("provider unavailable")
    outcome = await AgentRunner(MiniConfig(), provider=provider, runs_dir=tmp_path).run_and_capture(
        "read the web", system_prompt_override="CUSTOM ROLE",
    )
    assert outcome.status == ("failed" if failure else "success")
    assert len(instances) == 1
    instances[0].close.assert_awaited_once()
    prompt = provider.chat.call_args.kwargs["system"]
    assert "CUSTOM ROLE" in prompt
    assert "web_search" in prompt and "web_fetch" in prompt
    assert "untrusted" in prompt


# 功能：取消主任务时等待浏览器关闭后传播取消
# 设计：用事件保证已进入模型调用，避免时间睡眠导致竞态
async def test_cancelled_root_closes_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[AsyncMock] = []
    monkeypatch.setattr(
        "mini_claude.core.runner.BrowserSession", _browser_factory(instances), raising=False,
    )
    entered = asyncio.Event()

    # 通知调用已经开始并阻塞到测试取消
    async def wait_for_cancel(**kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    provider = AsyncMock()
    provider.chat.side_effect = wait_for_cancel
    task = asyncio.create_task(
        AgentRunner(MiniConfig(), provider=provider, runs_dir=tmp_path).run("read the web"),
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(instances) == 1
    instances[0].close.assert_awaited_once()


# 功能：角色子代理均可搜索和抓取，并且每个任务的浏览器独立释放
# 设计：执行真实子代理流程，检查发送给模型的 schema 和资源实例
async def test_children_receive_network_tools_and_own_browsers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[AsyncMock] = []
    monkeypatch.setattr(
        "mini_claude.core.subagent.tool.BrowserSession", _browser_factory(instances), raising=False,
    )
    provider = AsyncMock()
    provider.chat.return_value = LlmResponse(stop_reason="end_turn", text="done")
    tool = SpawnAgentTool(
        provider, EventBus(), "parent", None, 5, BackgroundTaskRegistry(), tmp_path, "session",
    )
    for role in ("planner", "reviewer", "executor"):
        result = await tool.invoke({"description": "research", "prompt": "read", "subagent_type": role})
        assert not result.is_error
        names = {item["name"] for item in provider.chat.call_args.kwargs["tool_schemas"]}
        assert {"web_search", "web_fetch"} <= names
        profile = AgentProfileLoader().load(role)
        if role != "executor":
            assert "browser_click" not in profile.allowed_tools
    assert len(instances) == 3
    assert len({id(item) for item in instances}) == 3
    for browser in instances:
        browser.close.assert_awaited_once()
