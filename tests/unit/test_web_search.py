from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from mini_claude.core.tools.builtin.web_search import WebSearchTool


# 功能：验证搜索在后台线程执行并返回带来源、时间与外部内容提示的结构化摘要
# 设计：替换实际搜索客户端并记录线程，避免联网同时检查不会阻塞事件循环
async def test_search_returns_sources_off_event_loop() -> None:
    main_thread = threading.get_ident()
    threads: list[int] = []

    # 记录搜索所在线程并返回上游真实字段格式
    def search(*args: object, **kwargs: object) -> list[dict[str, str]]:
        threads.append(threading.get_ident())
        return [{"title": "Python", "href": "https://python.org/", "body": "Python docs"}]

    with patch("mini_claude.core.tools.builtin.web_search.DDGS") as client:
        client.return_value.text.side_effect = search
        result = await WebSearchTool().invoke({"query": "  Python docs  "})
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["results"] == [
        {"title": "Python", "url": "https://python.org/", "snippet": "Python docs"}
    ]
    assert payload["fetched_at"]
    assert "untrusted" in payload["notice"].lower()
    assert threads and threads[0] != main_thread
    client.assert_called_once_with(timeout=15)
    client.return_value.text.assert_called_once_with(
        "Python docs", max_results=5, backend="yahoo,duckduckgo,brave"
    )


# 功能：验证搜索结果数量、标题与摘要受到上限约束
# 设计：模拟上游忽略结果数量并返回超长文本，检查工具边界独立生效
async def test_search_bounds_upstream_output() -> None:
    rows = [{"title": "t" * 1000, "href": "https://example.com", "body": "s" * 5000}] * 20
    with patch("mini_claude.core.tools.builtin.web_search.DDGS") as client:
        client.return_value.text.return_value = rows
        result = await WebSearchTool().invoke({"query": "example", "max_results": 2})
    payload = json.loads(result.content)
    assert len(payload["results"]) == 2
    assert len(payload["results"][0]["title"]) <= 500
    assert len(payload["results"][0]["snippet"]) <= 1500


# 功能：验证零条结果与服务故障被清晰区分且异常不泄露凭据
# 设计：让同一上游桩先返回空列表再抛出含秘密的错误，检查成功与错误标志
async def test_search_empty_is_distinct_from_provider_error() -> None:
    with patch("mini_claude.core.tools.builtin.web_search.DDGS") as client:
        client.return_value.text.side_effect = [[], RuntimeError("proxy user:password")]
        empty = await WebSearchTool().invoke({"query": "empty"})
        failed = await WebSearchTool().invoke({"query": "failed"})
    assert not empty.is_error
    assert json.loads(empty.content)["status"] == "no_results"
    assert failed.is_error
    assert "password" not in failed.content


# 功能：验证无效搜索参数在调用任何提供者前被拒绝
# 设计：覆盖空白、超长查询与数量边界，直接检查上游完全未调用
@pytest.mark.parametrize(
    "params",
    [
        {"query": " "},
        {"query": "q" * 2001},
        {"query": "q", "max_results": 0},
        {"query": "q", "max_results": 11},
    ],
)
async def test_search_rejects_invalid_params(params: dict[str, object]) -> None:
    with patch("mini_claude.core.tools.builtin.web_search.DDGS", new=MagicMock()) as client:
        with pytest.raises(ValidationError):
            await WebSearchTool().invoke(params)
    client.assert_not_called()
