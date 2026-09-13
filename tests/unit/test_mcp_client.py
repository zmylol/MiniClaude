from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock

import pytest

from mini_claude.core.mcp.client import McpClient, McpServerUnavailableError, McpToolError


# 功能：验证 MCP isError 被传播为应用错误
# 设计：响应保持协议成功但工具失败，覆盖原客户端会误判成功的路径
async def test_call_tool_propagates_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = McpClient()
    monkeypatch.setattr(client, "_call", AsyncMock(return_value={
        "isError": True, "content": [{"type": "text", "text": "click failed"}],
    }))
    with pytest.raises(McpToolError, match="click failed"):
        await client.call_tool("browser_click", {})


# 功能：验证结构化结果不会因缺少 text 内容而丢失
# 设计：只返回 structuredContent，要求可解析 JSON 且保留原值
async def test_call_tool_preserves_structured_content(monkeypatch: pytest.MonkeyPatch) -> None:
    client = McpClient()
    monkeypatch.setattr(client, "_call", AsyncMock(return_value={
        "structuredContent": {"answer": 42, "ok": True}, "content": [],
    }))
    assert json.loads(await client.call_tool("query", {})) == {"answer": 42, "ok": True}


# 功能：验证仅包含不支持媒体的响应不会返回空成功
# 设计：模拟没有文本的截图结果，必须明确指出客户端不支持该内容类型
async def test_call_tool_rejects_unsupported_only_content(monkeypatch: pytest.MonkeyPatch) -> None:
    client = McpClient()
    monkeypatch.setattr(client, "_call", AsyncMock(return_value={
        "content": [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}],
    }))
    with pytest.raises(McpToolError, match="unsupported"):
        await client.call_tool("image", {})


# 功能：验证取消中的请求关闭连接，避免下一次调用读到迟到响应
# 设计：在实际请求读阶段挂起，取消后断言关闭并拒绝继续复用传输
async def test_cancelled_request_closes_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    client = McpClient()
    entered = asyncio.Event()

    # 模拟请求已发送但服务器还没有返回
    async def read() -> str:
        entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    monkeypatch.setattr(client, "_write_line", AsyncMock())
    monkeypatch.setattr(client, "_read_line", read)
    close = AsyncMock()
    monkeypatch.setattr(client, "close", close)
    task = asyncio.create_task(client.call_tool("submit", {}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    close.assert_awaited_once()


# 功能：验证关闭会回收当前 stdio 子进程且重复关闭无害
# 设计：启动真实本地进程隔离验证退出状态，不使用网络或浏览器安装
async def test_close_reaps_stdio_process(monkeypatch: pytest.MonkeyPatch) -> None:
    client = McpClient()
    monkeypatch.setattr(client, "_initialize", AsyncMock())
    await client.connect_stdio(sys.executable, ["-c", "import time; time.sleep(60)"])
    proc = client._proc
    assert proc is not None
    await client.close()
    await client.close()
    assert proc.returncode is not None
    with pytest.raises(McpServerUnavailableError):
        await client._write_line("{}")
