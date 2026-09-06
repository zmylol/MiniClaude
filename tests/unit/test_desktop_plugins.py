from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mini_claude.core.bus.envelope import HandlerError
from mini_claude.core.config import McpServerConfig
from mini_claude.core.desktop_services import ManagedMcpServers
from mini_claude.core.mcp.client import McpToolDef


# 功能：构造仅在内存完成握手和工具发现的 MCP 替身。
# 设计：禁用真实子进程与网络，测试生命周期和注册状态而不执行用户插件。
def client_stub() -> MagicMock:
    client = MagicMock()
    client.connect_stdio = AsyncMock()
    client.connect_tcp = AsyncMock()
    client.list_tools = AsyncMock(return_value=[McpToolDef(name="search", description="查找")])
    client.close = AsyncMock()
    return client


# 功能：插件列表只公开状态和工具名，不泄漏环境变量或启动参数。
# 设计：从带凭据的 TOML 配置启动替身，并精确比较公开字段。
async def test_plugin_list_hides_config_secrets(tmp_path: Path) -> None:
    client = client_stub()
    manager = ManagedMcpServers(tmp_path, is_busy=lambda: False, client_factory=lambda: client)
    await manager.start_all([
        McpServerConfig(name="docs", command="tool", args=["secret-arg"], env={"TOKEN": "secret"}),
    ])
    result = await manager.list_plugins({})
    assert result.model_dump() == {"servers": [{
        "name": "docs", "transport": "stdio", "status": "connected",
        "tools": ["docs__search"], "managed": False,
    }]}
    assert "secret" not in result.model_dump_json()
    await manager.stop_all()


# 功能：新增插件真实连接并注册工具，禁用后关闭连接并移除工具。
# 设计：使用配置文件往返检验重启后的禁用状态，不改写项目 TOML。
async def test_add_disable_reload_and_remove_managed_plugin(tmp_path: Path) -> None:
    client = client_stub()
    manager = ManagedMcpServers(tmp_path, is_busy=lambda: False, client_factory=lambda: client)
    await manager.start_all([])
    await manager.add_plugin({"name": "docs", "transport": "stdio", "command": "tool", "args": []})
    assert [tool.name for tool in manager.get_tools()] == ["docs__search"]
    client.connect_stdio.assert_awaited_once_with("tool", [], None)

    await manager.set_enabled({"name": "docs", "enabled": False})
    client.close.assert_awaited_once()
    assert manager.get_tools() == []
    saved = json.loads((tmp_path / ".mini/desktop_mcp.json").read_text())
    assert saved["enabled"]["docs"] is False

    second = ManagedMcpServers(tmp_path, is_busy=lambda: False, client_factory=client_stub)
    await second.start_all([])
    assert (await second.list_plugins({})).servers[0].status == "disabled"
    await second.remove_plugin({"name": "docs"})
    assert (await second.list_plugins({})).servers == []
    assert not (tmp_path / ".mini/config.toml").exists()


# 功能：正在运行任务时禁止修改插件连接与工具集。
# 设计：检查拒绝路径没有启动插件，也没有落盘配置。
async def test_busy_core_rejects_plugin_changes(tmp_path: Path) -> None:
    client = client_stub()
    manager = ManagedMcpServers(tmp_path, is_busy=lambda: True, client_factory=lambda: client)
    with pytest.raises(HandlerError, match="任务"):
        await manager.add_plugin({"name": "docs", "transport": "stdio", "command": "tool"})
    client.connect_stdio.assert_not_awaited()
    assert not (tmp_path / ".mini/desktop_mcp.json").exists()


# 功能：连接失败会关闭半初始化客户端并保留可重试的错误状态。
# 设计：初始化失败替身确保不会留下子进程、脏工具或永久修改标记。
async def test_failed_plugin_start_is_closed_and_visible(tmp_path: Path) -> None:
    client = client_stub()
    client.connect_stdio.side_effect = RuntimeError("token=secret")
    manager = ManagedMcpServers(tmp_path, is_busy=lambda: False, client_factory=lambda: client)
    result = await manager.add_plugin({"name": "docs", "transport": "stdio", "command": "tool"})
    assert result.servers[0].status == "error"
    assert "secret" not in result.model_dump_json()
    client.close.assert_awaited_once()
    assert manager.get_tools() == []
    assert manager.changing is False


# 功能：用户 TOML 定义的插件不能通过桌面删除。
# 设计：检查删除请求保留原始配置和仍在使用的连接。
async def test_configured_plugin_cannot_be_removed(tmp_path: Path) -> None:
    client = client_stub()
    manager = ManagedMcpServers(tmp_path, is_busy=lambda: False, client_factory=lambda: client)
    await manager.start_all([McpServerConfig(name="docs", command="tool")])
    with pytest.raises(HandlerError, match="配置"):
        await manager.remove_plugin({"name": "docs"})
    client.close.assert_not_awaited()
    await manager.stop_all()


# 功能：插件连接尚未完成期间，修改标记保持有效并拒绝并发变更。
# 设计：暂停替身握手以覆盖异步等待窗口，避免运行入口读到失效守卫。
async def test_plugin_change_guard_covers_connection_wait(tmp_path: Path) -> None:
    client = client_stub()
    connected = asyncio.Event()
    release = asyncio.Event()

    # 功能：在握手开始后暂停，让测试能够检查并发修改边界。
    # 设计：通过事件同步替代固定等待，保证测试不依赖机器速度。
    async def connect(command: str, args: list[str], env: dict[str, str] | None) -> None:
        connected.set()
        await release.wait()

    client.connect_stdio.side_effect = connect
    manager = ManagedMcpServers(tmp_path, is_busy=lambda: False, client_factory=lambda: client)
    adding = asyncio.create_task(manager.add_plugin({
        "name": "docs", "transport": "stdio", "command": "tool",
    }))
    await connected.wait()
    assert manager.changing is True
    with pytest.raises(HandlerError):
        await manager.set_enabled({"name": "docs", "enabled": False})
    release.set()
    await adding
    assert manager.changing is False
    await manager.stop_all()
