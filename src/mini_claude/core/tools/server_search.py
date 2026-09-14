from __future__ import annotations

from typing import TYPE_CHECKING

from mini_claude.core.tools.builtin.web_search import WebSearchTool

if TYPE_CHECKING:
    from mini_claude.core.llm.base import LLMProvider
    from mini_claude.core.tools.registry import ToolRegistry


# 按实际 provider 能力注册唯一搜索定义，权限由每次请求发送前统一检查
def register_web_search(
    registry: ToolRegistry,
    provider: LLMProvider | None,
) -> None:
    if getattr(provider, "server_search_supported", False) is True:
        registry.register_server_tool({
            "type": "web_search_20250305", "name": "web_search", "max_uses": 5,
        })
    else:
        registry.register(WebSearchTool())
