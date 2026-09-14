from __future__ import annotations

from copy import deepcopy

from mini_claude.core.tools.base import BaseTool


class ToolRegistry:
    # 初始化本地执行器和服务端工具定义
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._server_tools: dict[str, dict[str, object]] = {}

    # 注册本地工具；本地同名覆盖，已注册的服务端工具优先
    def register(self, tool: BaseTool) -> None:
        if tool.name not in self._server_tools:
            self._tools[tool.name] = tool

    # 注册服务端原生定义并移除同名本地执行器，防止搜索重复暴露或执行
    def register_server_tool(self, schema: dict[str, object]) -> None:
        name = str(schema["name"])
        self._tools.pop(name, None)
        self._server_tools[name] = deepcopy(schema)

    # 按名称查找工具，不存在返回 None
    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    # 返回所有工具的 Anthropic 格式 schema 列表
    def tool_schemas(self) -> list[dict[str, object]]:
        schemas: list[dict[str, object]] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]
        return schemas + deepcopy(list(self._server_tools.values()))
