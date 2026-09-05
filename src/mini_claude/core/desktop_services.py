from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mini_claude.core.bus.desktop_commands import (
    PluginInfo,
    PluginsAddCommand,
    PluginsListCommand,
    PluginsRemoveCommand,
    PluginsResult,
    PluginsSetEnabledCommand,
)
from mini_claude.core.bus.envelope import HandlerError
from mini_claude.core.config import McpServerConfig
from mini_claude.core.mcp.client import McpClient
from mini_claude.core.mcp.server import McpServerManager
from mini_claude.core.mcp.tool import McpTool
from mini_claude.core.transport.socket_server import SocketServer

logger = logging.getLogger(__name__)


class PluginStore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    servers: list[PluginsAddCommand] = Field(default_factory=list)
    enabled: dict[str, bool] = Field(default_factory=dict)


class ManagedMcpServers(McpServerManager):
    # 在原有 MCP 管理器接口上增加项目持久化及运行期间的修改互斥。
    def __init__(
        self, project_path: Path, is_busy: Callable[[], bool],
        client_factory: Callable[[], McpClient] = McpClient,
    ) -> None:
        super().__init__()
        self._path = project_path / ".mini" / "desktop_mcp.json"
        self._is_busy = is_busy
        self._client_factory = client_factory
        self._configs: dict[str, McpServerConfig] = {}
        self._managed: set[str] = set()
        self._enabled: dict[str, bool] = {}
        self._server_tools: dict[str, list[McpTool]] = {}
        self.changing = False

    # 叠加桌面维护的插件与启用状态，保留用户 TOML 配置及其中的环境变量。
    async def start_all(self, servers: list[McpServerConfig]) -> None:
        self._configs = {config.name: config for config in servers}
        if len(self._configs) != len(servers):
            raise ValueError("MCP 配置中存在重复插件名称")
        saved = PluginStore()
        if self._path.exists():
            saved = PluginStore.model_validate_json(self._path.read_text())
        for entry in saved.servers:
            if entry.name in self._configs:
                raise ValueError(f"桌面插件与项目配置名称冲突：{entry.name}")
            self._configs[entry.name] = McpServerConfig(**entry.model_dump(exclude={"type"}))
            self._managed.add(entry.name)
        self._enabled = saved.enabled
        for name in self._configs:
            if self._enabled.get(name, True):
                await self._start_plugin(name)

    # 验证没有运行中的任务并锁住新任务入口，防止关闭仍被工具调用使用的连接。
    def _begin_change(self) -> None:
        if self.changing or self._is_busy():
            raise HandlerError(-32040, "任务运行或插件更新期间不能修改插件，请稍后重试")
        self.changing = True

    # 只写桌面插件及启用覆盖值，使用原子替换避免异常留下半份配置。
    def _persist(
        self, configs: dict[str, McpServerConfig], managed: set[str], enabled: dict[str, bool],
    ) -> None:
        entries = [{
            "name": configs[name].name, "transport": configs[name].transport,
            "command": configs[name].command, "args": configs[name].args,
            "host": configs[name].host, "port": configs[name].port,
        } for name in sorted(managed)]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=self._path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump({"servers": entries, "enabled": enabled}, stream, ensure_ascii=False)
                stream.flush()
                temporary.replace(self._path)
            finally:
                temporary.unlink(missing_ok=True)
        self._configs, self._managed, self._enabled = configs, managed, enabled

    # 连接和发现全部成功后一次性公开工具，失败时关闭半初始化客户端。
    async def _start_plugin(self, name: str) -> None:
        if name in self._clients:
            return
        config = self._configs[name]
        client = self._client_factory()
        try:
            if config.transport == "stdio":
                await asyncio.wait_for(
                    client.connect_stdio(config.command, config.args, config.env or None),
                    timeout=15,
                )
            else:
                await asyncio.wait_for(client.connect_tcp(config.host, config.port), timeout=15)
            definitions = await asyncio.wait_for(client.list_tools(), timeout=15)
        except BaseException as exc:
            await client.close()
            if not isinstance(exc, Exception):
                raise
            logger.warning("MCP 插件 %s 启动失败，可在桌面重试启用", name)
            return
        self._clients[name] = client
        self._server_tools[name] = [McpTool(client, name, item) for item in definitions]
        self._tools = [tool for group in self._server_tools.values() for tool in group]

    # 从工具快照移除已禁用插件，再关闭该插件自己的连接。
    async def _stop_plugin(self, name: str) -> None:
        self._server_tools.pop(name, None)
        self._tools = [tool for group in self._server_tools.values() for tool in group]
        client = self._clients.pop(name, None)
        if client is not None:
            await client.close()

    # 返回界面所需的有限字段，不公开命令参数、环境变量或服务端异常内容。
    async def list_plugins(self, params: dict[str, Any]) -> PluginsResult:
        PluginsListCommand.model_validate(params)
        servers = []
        for name, config in self._configs.items():
            status = "connected" if name in self._clients else "error"
            if not self._enabled.get(name, True):
                status = "disabled"
            servers.append(PluginInfo.model_validate({
                "name": name, "transport": config.transport, "status": status,
                "tools": [tool.name for tool in self._server_tools.get(name, [])],
                "managed": name in self._managed,
            }))
        return PluginsResult(servers=servers)

    # 持久化用户新增的插件并连接，连接失败仍保留可重试的配置条目。
    async def add_plugin(self, params: dict[str, Any]) -> PluginsResult:
        command = PluginsAddCommand.model_validate(params)
        self._begin_change()
        try:
            if command.name in self._configs:
                raise HandlerError(-32041, "插件名称已经存在")
            configs = dict(self._configs)
            configs[command.name] = McpServerConfig(**command.model_dump(exclude={"type"}))
            self._persist(
                configs, self._managed | {command.name}, {**self._enabled, command.name: True},
            )
            await self._start_plugin(command.name)
            return await self.list_plugins({})
        finally:
            self.changing = False

    # 保存启用状态并实际建立或关闭连接，允许对失败插件重新发起连接。
    async def set_enabled(self, params: dict[str, Any]) -> PluginsResult:
        command = PluginsSetEnabledCommand.model_validate(params)
        self._begin_change()
        try:
            if command.name not in self._configs:
                raise HandlerError(-32041, "插件不存在")
            self._persist(
                dict(self._configs), set(self._managed),
                {**self._enabled, command.name: command.enabled},
            )
            if command.enabled:
                await self._start_plugin(command.name)
            else:
                await self._stop_plugin(command.name)
            return await self.list_plugins({})
        finally:
            self.changing = False

    # 仅允许删除桌面创建的插件，保留用户配置文件中的插件定义。
    async def remove_plugin(self, params: dict[str, Any]) -> PluginsResult:
        command = PluginsRemoveCommand.model_validate(params)
        self._begin_change()
        try:
            if command.name not in self._managed:
                raise HandlerError(-32041, "只能移除桌面添加的插件，配置文件插件可禁用")
            configs = {name: cfg for name, cfg in self._configs.items() if name != command.name}
            enabled = {name: value for name, value in self._enabled.items() if name != command.name}
            self._persist(configs, self._managed - {command.name}, enabled)
            await self._stop_plugin(command.name)
            return await self.list_plugins({})
        finally:
            self.changing = False

    # 关闭全部插件并清空工具快照，避免关闭后的客户端再进入后续运行。
    async def stop_all(self) -> None:
        await super().stop_all()
        self._server_tools.clear()
        self._tools.clear()


# 把桌面插件命令注册到现有 JSON-RPC 服务，继续复用同一事件流连接。
def register_plugins(server: SocketServer, manager: ManagedMcpServers) -> None:
    server.register("plugins.list", manager.list_plugins)
    server.register("plugins.add", manager.add_plugin)
    server.register("plugins.set_enabled", manager.set_enabled)
    server.register("plugins.remove", manager.remove_plugin)
