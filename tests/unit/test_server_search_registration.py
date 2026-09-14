from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mini_claude.core.agents.loader import AgentProfile
from mini_claude.core.config import MiniConfig
from mini_claude.core.events.bus import EventBus
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from mini_claude.core.runner import AgentRunner
from mini_claude.core.subagent.registry import BackgroundTaskRegistry
from mini_claude.core.subagent.tool import SpawnAgentTool
from mini_claude.core.task.manager import TaskManager
from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.registry import ToolRegistry


class _BrowserTool(BaseTool):
    name = "browser_snapshot"
    description = "A browser tool for registration tests"
    input_schema = {"type": "object", "properties": {}}

    # 提供不会启动浏览器的本地执行器
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult("snapshot")


# 复用主运行和子代理的真实组装路径，仅替换不应联网的模型与浏览器边界
def _registry(
    path: Path,
    scope: str,
    *,
    native: bool,
    enabled: bool = True,
    allowed_tools: list[str] | None = None,
    permissions: PermissionManager | None = None,
) -> ToolRegistry:
    provider = AsyncMock()
    provider.server_search_supported = native
    browser = AsyncMock()
    browser.get_tools = lambda: [_BrowserTool()]
    config = MiniConfig()
    config.network.enabled = enabled
    if scope == "root":
        runner = AgentRunner(
            config, provider=provider, runs_dir=path, permission_manager=permissions,
        )
        return runner._build_registry(
            TaskManager(path / "tasks"), provider=provider,
            tool_whitelist=allowed_tools, browser=browser, session_id="session",
        )
    tool = SpawnAgentTool(
        provider, EventBus(), "parent", permissions, 5,
        BackgroundTaskRegistry(), path, "session", network_config=config.network,
    )
    profile = (
        AgentProfile("limited", "limited role", "", allowed_tools=allowed_tools)
        if allowed_tools is not None else None
    )
    return tool._build_child_registry(EventBus(), "child", profile, browser=browser)


@pytest.mark.parametrize("scope", ["root", "child"])
# 功能：DeepSeek 主代理和子代理只暴露一个原生搜索定义，抓取与浏览器仍在本地执行
# 设计：检查模型 schema 和执行器两个边界，防止服务端搜索与 DDGS 同时注册
def test_native_search_replaces_local_search_only(tmp_path: Path, scope: str) -> None:
    registry = _registry(tmp_path, scope, native=True)
    searches = [item for item in registry.tool_schemas() if item["name"] == "web_search"]
    assert searches == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    assert registry.get("web_search") is None
    assert registry.get("web_fetch") is not None
    assert registry.get("browser_snapshot") is not None


@pytest.mark.parametrize("scope", ["root", "child"])
# 功能：不支持原生搜索的后端继续获得原有本地搜索工具
# 设计：关闭 provider 能力标记，确保迁移不破坏 Anthropic 或兼容代理已有的联网行为
def test_non_native_provider_keeps_local_search(tmp_path: Path, scope: str) -> None:
    registry = _registry(tmp_path, scope, native=False)
    searches = [item for item in registry.tool_schemas() if item["name"] == "web_search"]
    assert len(searches) == 1
    assert "input_schema" in searches[0]
    assert registry.get("web_search") is not None


@pytest.mark.parametrize("scope", ["root", "child"])
# 功能：网络总开关同时禁用原生搜索、本地抓取和浏览器定义
# 设计：对已声明原生能力的 provider 关闭网络，检查实际发给模型的全部 schema
def test_network_disabled_omits_native_search(tmp_path: Path, scope: str) -> None:
    registry = _registry(tmp_path, scope, native=True, enabled=False)
    names = {item["name"] for item in registry.tool_schemas()}
    assert not {"web_search", "web_fetch", "browser_snapshot"} & names


@pytest.mark.parametrize("scope", ["root", "child"])
@pytest.mark.parametrize("allowed_tools", [["web_fetch"], ["web_search"]])
# 功能：主任务和角色子代理的白名单限制服务端与本地网络工具
# 设计：分别允许抓取和搜索，断言 schema 名称完全匹配白名单且原生搜索保留服务端类型
def test_whitelist_applies_to_native_search(
    tmp_path: Path, scope: str, allowed_tools: list[str],
) -> None:
    registry = _registry(tmp_path, scope, native=True, allowed_tools=allowed_tools)
    schemas = registry.tool_schemas()
    assert {item["name"] for item in schemas} == set(allowed_tools)
    if "web_search" in allowed_tools:
        assert schemas == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
        assert registry.get("web_search") is None


@pytest.mark.parametrize("scope", ["root", "child"])
@pytest.mark.parametrize("decision", [PermissionDecision.DENY, PermissionDecision.ASK])
# 功能：需要审批或已禁止搜索时，主代理和子代理都不暴露原生搜索，也不退回 DDGS
# 设计：服务端执行无法等待本地逐次审批，必须在发送 schema 前执行已有权限策略
def test_native_search_requires_permission_before_registration(
    tmp_path: Path, scope: str, decision: PermissionDecision,
) -> None:
    permissions = PermissionManager({"web_search": ToolPolicy(default=decision)})
    registry = _registry(tmp_path, scope, native=True, permissions=permissions)
    assert "web_search" not in {item["name"] for item in registry.tool_schemas()}
    assert registry.get("web_search") is None


@pytest.mark.parametrize("scope", ["root", "child"])
# 功能：持久化的始终拒绝搜索同时限制主代理和角色子代理
# 设计：通过审批响应处理路径写入临时策略文件，再重建管理器验证跨会话拒绝不会被原生搜索绕过
def test_native_search_respects_persisted_denial(tmp_path: Path, scope: str) -> None:
    policy_file = tmp_path / "permissions.toml"
    manager = PermissionManager(policy_file=policy_file)
    manager._apply_response("always_deny", "old-session", "web_search")
    restored = PermissionManager(policy_file=policy_file)
    registry = _registry(tmp_path, scope, native=True, permissions=restored)
    assert "web_search" not in {item["name"] for item in registry.tool_schemas()}
