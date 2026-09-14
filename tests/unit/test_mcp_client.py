from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock

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


# 功能：验证取消普通共享 MCP 请求后，后续调用仍能跳过迟到结果继续工作
# 设计：先阻塞并取消第一次读取，再返回旧响应和新响应，避免浏览器清理策略影响专用连接器
async def test_cancelled_shared_request_keeps_transport_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = McpClient()
    entered = asyncio.Event()
    responses = iter([
        {"id": 1, "result": {"content": [{"type": "text", "text": "old action"}]}},
        {"id": 2, "result": {"content": [{"type": "text", "text": "new result"}]}},
    ])

    # 第一次读取被取消后模拟前后两次响应依次到达
    async def read() -> str:
        if not entered.is_set():
            entered.set()
            await asyncio.Event().wait()
        return json.dumps(next(responses))

    monkeypatch.setattr(client, "_write_line", AsyncMock())
    monkeypatch.setattr(client, "_read_line", read)
    close = AsyncMock()
    monkeypatch.setattr(client, "close", close)
    task = asyncio.create_task(client.call_tool("submit", {}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await client.call_tool("query", {}) == "new result"
    close.assert_not_awaited()


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


# 功能：验证启动进程的等待被取消时也接管并回收已经启动的进程
# 设计：在真实本地子进程启动后延迟返回句柄，精确覆盖取消与句柄赋值之间的竞态
async def test_cancelled_startup_reaps_created_process(monkeypatch: pytest.MonkeyPatch) -> None:
    original = asyncio.create_subprocess_exec
    entered = asyncio.Event()
    release = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []

    # 启动真实进程后保留返回值直到测试允许握手续行
    async def spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        proc = await original(*args, **kwargs)
        processes.append(proc)
        entered.set()
        await release.wait()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    client = McpClient()
    task = asyncio.create_task(client.connect_stdio(sys.executable, ["-c", "import time; time.sleep(60)"]))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None
    await client.close()


# 功能：验证普通共享客户端保留连接错误供上层生命周期负责人处理
# 设计：协议读阶段返回超时，确保专用连接器不会被浏览器的清理策略永久关闭
async def test_transport_error_is_reported_to_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    client = McpClient()
    monkeypatch.setattr(client, "_write_line", AsyncMock())
    monkeypatch.setattr(client, "_read_line", AsyncMock(side_effect=McpServerUnavailableError("timeout")))
    close = AsyncMock()
    monkeypatch.setattr(client, "close", close)
    with pytest.raises(McpServerUnavailableError, match="timeout"):
        await client.call_tool("submit", {})
    close.assert_not_awaited()


# 功能：验证实际 stdio 上的 MCP 握手、工具发现、通知过滤及应用错误完整往返
# 设计：使用无依赖本地协议服务验证真实字节边界，避免所有测试模拟私有方法而漏掉通信回归
async def test_stdio_protocol_roundtrip() -> None:
    program = '''
import json
import sys
initialized = False
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    if method == 'notifications/initialized':
        initialized = True
        continue
    if method == 'initialize':
        result = {'protocolVersion': '2024-11-05', 'capabilities': {}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
    elif method == 'tools/list':
        assert initialized
        result = {'tools': [{'name': 'echo', 'description': 'Echo text', 'inputSchema': {'type': 'object'}}]}
    else:
        print('startup diagnostic', flush=True)
        print(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/message', 'params': {}}), flush=True)
        if request['params']['name'] == 'fail':
            print(json.dumps({'jsonrpc': '2.0', 'id': str(request['id']), 'error': {'code': -32602, 'message': 'invalid argument'}}), flush=True)
            continue
        result = {'content': [{'type': 'text', 'text': request['params']['arguments']['text']}]}
    print(json.dumps({'jsonrpc': '2.0', 'id': str(request['id']), 'result': result}), flush=True)
'''
    client = McpClient()
    try:
        await client.connect_stdio(sys.executable, ["-u", "-c", program])
        definitions = await client.list_tools()
        assert len(definitions) == 1 and definitions[0].name == "echo"
        assert await client.call_tool("echo", {"text": "hello"}) == "hello"
        with pytest.raises(McpToolError, match="invalid argument"):
            await client.call_tool("fail", {})
        assert await client.call_tool("echo", {"text": "still connected"}) == "still connected"
    finally:
        await client.close()


# 功能：TCP MCP 在空闲收到 EOF 或本地 writer 关闭后也不会继续报告连接有效
# 设计：真实 StreamReader 注入 EOF 并替换无网络 writer，覆盖无需另一次工具调用的断开状态
async def test_tcp_idle_eof_and_closing_writer_invalidate_connection() -> None:
    client = McpClient()
    reader = asyncio.StreamReader()
    writer = MagicMock()
    writer.is_closing.return_value = False
    client._transport = "tcp"
    client._reader = reader
    client._tcp_writer = writer
    assert client.connected
    writer.is_closing.return_value = True
    assert not client.connected
    writer.is_closing.return_value = False
    reader.feed_eof()
    assert not client.connected


# 功能：TCP 服务发送未消费通知后关闭连接，插件状态仍能立即检测 EOF
# 设计：真实本地 TCP 服务只发送通知再关闭，等待传输接收 EOF 而不读取缓冲区以复现 at_eof 的遗漏
async def test_tcp_eof_with_unread_notification_is_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    # 向客户端发送一条无需响应的通知后关闭 TCP 传输
    async def peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b'{"jsonrpc":"2.0","method":"notifications/message"}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    client = McpClient()
    monkeypatch.setattr(client, "_initialize", AsyncMock())
    try:
        await client.connect_tcp("127.0.0.1", server.sockets[0].getsockname()[1])

        # 仅同步传输到达 EOF 的时刻，确保测试本身不消费待处理的通知
        async def wait_for_eof() -> None:
            while not getattr(client._reader, "_eof", False):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_eof(), 1)
        assert client._reader is not None and not client._reader.at_eof()
        assert not client.connected
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
