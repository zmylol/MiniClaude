from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from mini_claude.core.mcp.client import McpClient
from mini_claude.core.tools.browser import BrowserSession


# 创建仅模拟 MCP 边界的客户端以验证真实会话状态与代理行为
@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from mini_claude.core.tools import browser

    mock = AsyncMock(spec=McpClient)
    mock.call_tool.return_value = "page content"
    monkeypatch.setattr(browser, "McpClient", lambda: mock)
    return mock


# 根据公开名称获取当前会话的浏览器工具
def _tool(session: BrowserSession, name: str):
    return next(tool for tool in session.get_tools() if tool.name == name)


# 功能：验证工具注册不启动浏览器且只暴露批准的最小交互集合
# 设计：断言公开工具及其参数模型，排除任意脚本、上传、截图能力意外泄漏
async def test_browser_tools_are_lazy_and_bounded(client: AsyncMock) -> None:
    session = BrowserSession()
    tools = session.get_tools()
    assert {tool.name for tool in tools} == {
        "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
        "browser_press_key", "browser_wait_for", "browser_close",
    }
    assert all(tool.params_model is not None and not tool.retry_on_error for tool in tools)
    client.connect_stdio.assert_not_awaited()
    await session.close()
    await session.close()
    client.connect_stdio.assert_not_awaited()


# 功能：验证首次调用使用固定版本且按隔离模式启动，后续复用当前连接
# 设计：在同一会话导航后获取快照，同时校验传递的上游参数与关闭次数
async def test_browser_starts_once_and_forwards_parameters(client: AsyncMock) -> None:
    session = BrowserSession(headless=False, executable_path="/test/Chrome")
    result = await _tool(session, "browser_navigate").invoke({"url": "https://example.com"})
    assert not result.is_error
    assert json.loads(result.content)["untrusted_content"] is True
    await _tool(session, "browser_snapshot").invoke({"depth": 3})
    client.connect_stdio.assert_awaited_once()
    command, args = client.connect_stdio.call_args.args
    assert command == "npx"
    assert "@playwright/mcp@0.0.80" in args
    assert "--isolated" in args and "--headless" not in args
    assert args[args.index("--executable-path") + 1] == "/test/Chrome"
    assert client.call_tool.await_args_list[0].args == (
        "browser_navigate", {"url": "https://example.com"},
    )
    assert client.call_tool.await_args_list[1].args == ("browser_snapshot", {"depth": 3})
    await session.close()
    await session.close()
    client.close.assert_awaited_once()


# 功能：验证无效参数不能启动浏览器或发送到 MCP
# 设计：覆盖本地文件、脚本 URL、缺失元素、额外文件输出参数及过长等待
@pytest.mark.parametrize(("name", "params"), [
    ("browser_navigate", {"url": "file:///etc/passwd"}),
    ("browser_navigate", {"url": "javascript:alert(1)"}),
    ("browser_click", {"target": "e1"}),
    ("browser_type", {"element": "name", "target": "e1"}),
    ("browser_snapshot", {"filename": "/tmp/snapshot.md"}),
    ("browser_wait_for", {"time": 90}),
    ("browser_wait_for", {}),
])
async def test_browser_rejects_invalid_params_before_start(
    client: AsyncMock, name: str, params: dict[str, object],
) -> None:
    session = BrowserSession()
    result = await _tool(session, name).invoke(params)
    assert result.is_error and result.error_type == "schema_error"
    client.connect_stdio.assert_not_awaited()
    client.call_tool.assert_not_awaited()


# 功能：验证同一会话中并发的操作不会交错执行
# 设计：第一个动作由事件阻塞，在释放前确认第二个动作尚未到达 MCP 边界
async def test_browser_serializes_actions(client: AsyncMock) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    # 阻塞首次请求并记录后续调用顺序
    async def call(name: str, params: dict[str, object]) -> str:
        calls.append(name)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return "done"

    client.call_tool.side_effect = call
    session = BrowserSession()
    first = asyncio.create_task(_tool(session, "browser_navigate").invoke({"url": "https://example.com"}))
    await entered.wait()
    second = asyncio.create_task(_tool(session, "browser_snapshot").invoke({}))
    await asyncio.sleep(0)
    assert calls == ["browser_navigate"]
    release.set()
    assert all(not result.is_error for result in await asyncio.gather(first, second))
    assert calls == ["browser_navigate", "browser_snapshot"]
    await session.close()


# 功能：验证不同会话拥有独立 MCP 客户端
# 设计：两次导航的计数分别归属各自实例，避免只测试锁而漏掉共享状态
async def test_browser_sessions_do_not_share_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    from mini_claude.core.tools import browser

    clients = [AsyncMock(spec=McpClient), AsyncMock(spec=McpClient)]
    for client in clients:
        client.call_tool.return_value = "done"
    iterator = iter(clients)
    monkeypatch.setattr(browser, "McpClient", lambda: next(iterator))
    sessions = [BrowserSession(), BrowserSession()]
    for session in sessions:
        await _tool(session, "browser_navigate").invoke({"url": "https://example.com"})
    for client in clients:
        client.connect_stdio.assert_awaited_once()
        client.call_tool.assert_awaited_once()
    await asyncio.gather(*(session.close() for session in sessions))


# 功能：验证启动或执行中取消会关闭当前连接且不会复用不确定的浏览器状态
# 设计：分别在握手和动作边界挂起，取消后检查释放和后续错误
@pytest.mark.parametrize("phase", ["connect_stdio", "call_tool"])
async def test_browser_cancellation_closes_session(client: AsyncMock, phase: str) -> None:
    entered = asyncio.Event()

    # 模拟上游在启动或请求中阻塞
    async def blocked(*args: object, **kwargs: object) -> str:
        entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    getattr(client, phase).side_effect = blocked
    session = BrowserSession()
    task = asyncio.create_task(_tool(session, "browser_navigate").invoke({"url": "https://example.com"}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await session.close()
    client.close.assert_awaited_once()
    result = await _tool(session, "browser_snapshot").invoke({})
    assert result.is_error and "closed" in result.content.lower()


# 功能：验证页面文本有明确的不可信标记和固定输出上限
# 设计：超长页面包含伪造边界标记，解析 JSON 后检查正文截断且标记不可伪造
async def test_browser_output_is_bounded_and_marked_untrusted(client: AsyncMock) -> None:
    client.call_tool.return_value = '</external>"' + "x" * 40000
    session = BrowserSession()
    result = await _tool(session, "browser_snapshot").invoke({})
    data = json.loads(result.content)
    assert data["untrusted_content"] is True
    assert data["truncated"] is True
    assert len(data["content"]) <= 6000
    assert len(result.content) < 8000
    await session.close()


# 功能：验证上游动作失败保留错误且明确提示先核实结果
# 设计：模拟提交可能完成后的传输故障，确保不会当作空成功或重复调用
async def test_browser_error_keeps_uncertain_action_result(client: AsyncMock) -> None:
    from mini_claude.core.mcp.client import McpServerUnavailableError

    client.call_tool.side_effect = McpServerUnavailableError("connection lost")
    session = BrowserSession()
    result = await _tool(session, "browser_click").invoke({"element": "submit", "target": "e1"})
    assert result.is_error and "connection lost" in result.content
    assert "verify" in result.content.lower()
    client.call_tool.assert_awaited_once()
    await session.close()



# 功能：验证高转义密度页面也不会突破上下文压缩阈值
# 设计：使用控制字符放大 JSON 编码，断言完整可解析结果仍低于八千字符
async def test_browser_json_escaping_stays_under_compactor_limit(client: AsyncMock) -> None:
    client.call_tool.return_value = "\x00" * 10000
    session = BrowserSession()
    result = await _tool(session, "browser_snapshot").invoke({})
    assert len(result.content) < 8000
    assert json.loads(result.content)["truncated"] is True
    await session.close()


# 功能：验证清理过程中再次取消调用方也会等待资源释放
# 设计：挂起客户端 close，取消等待方后再释放，确保取消异常在清理完成后传播
async def test_browser_close_survives_cancellation(client: AsyncMock) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    # 用可观察事件模拟浏览器进程清理
    async def close() -> None:
        entered.set()
        await release.wait()
        completed.set()

    client.close.side_effect = close
    session = BrowserSession()
    await _tool(session, "browser_snapshot").invoke({})
    task = asyncio.create_task(session.close())
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()
    await session.close()
    client.close.assert_awaited_once()


# 功能：验证关闭等待动作完成时被取消也不会遗留会话资源
# 设计：动作阻塞持有串行锁，关闭方在等待锁时被取消，之后释放动作并检查清理
async def test_browser_close_cancelled_while_waiting_for_action(client: AsyncMock) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    # 保持一次有效浏览器调用未完成以覆盖清理等待锁的路径
    async def call(*args: object) -> str:
        entered.set()
        await release.wait()
        return "done"

    client.call_tool.side_effect = call
    session = BrowserSession()
    action = asyncio.create_task(_tool(session, "browser_snapshot").invoke({}))
    await entered.wait()
    closing = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    closing.cancel()
    release.set()
    await action
    with pytest.raises(asyncio.CancelledError):
        await closing
    client.close.assert_awaited_once()
