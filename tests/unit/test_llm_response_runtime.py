from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mini_claude.core.compact.compactor import Compactor
from mini_claude.core.context import ExecutionContext
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from mini_claude.core.loop import AgentLoop
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from mini_claude.core.tools.registry import ToolRegistry
from mini_claude.core.tools.server_search import register_web_search
from mini_claude.core.trace.provider import TracingProvider
from mini_claude.core.trace.writer import TraceWriter
from tests.unit.test_history_regressions import manager_for
from tests.unit.test_loop import _EchoTool


@pytest.mark.parametrize(
    ("stop_reason", "status", "reason"),
    [
        ("stop_sequence", "success", None),
        ("max_tokens", "failed", "max_tokens"),
        ("refusal", "failed", "refusal"),
        ("model_context_window_exceeded", "failed", "model_context_window_exceeded"),
        ("future_stop", "failed", "unsupported_stop_reason"),
    ],
)
# 功能：终止或截断响应保留文本并立即结束，不能重复请求直到耗尽步数
# 设计：让提供者始终返回同一停止原因，独立检查请求次数和用户能收到的状态与文本
async def test_terminal_stop_reason_does_not_repeat(
    stop_reason: str, status: str, reason: str | None,
) -> None:
    provider = AsyncMock()
    provider.chat.return_value = LlmResponse(stop_reason=stop_reason, text="kept answer")
    context = ExecutionContext(run_id="terminal", goal="goal", max_steps=3)

    await AgentLoop(provider, ToolRegistry(), EventBus()).run(context)

    assert provider.chat.await_count == 1
    assert (context.status, context.reason, context.result) == (status, reason, "kept answer")


# 功能：截断的工具调用不执行，并记录配对错误结果后结束
# 设计：不存在的工具也不能进入调用路径，通过历史结果和单次模型调用检查截断处理
async def test_truncated_tool_call_is_balanced_and_stops() -> None:
    provider = AsyncMock()
    provider.chat.return_value = LlmResponse(
        stop_reason="max_tokens", text="partial",
        tool_calls=[ToolCallBlock(id="tool-cut", name="write_file", input={})],
    )
    context = ExecutionContext(run_id="truncated", goal="goal", max_steps=3)

    await AgentLoop(provider, ToolRegistry(), EventBus()).run(context)

    assert provider.chat.await_count == 1
    assert context.reason == "max_tokens"
    result = context.messages[-1]["content"][0]
    assert result["tool_use_id"] == "tool-cut"
    assert result["is_error"] is True


# 功能：暂停的服务器搜索按原始块继续，结果不会当成本地工具执行
# 设计：捕获下一次请求的独立副本，并核对 thinking、搜索参数与签名未被重组或改写
async def test_paused_server_search_roundtrips_original_content() -> None:
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "opaque"},
        {"type": "text", "text": "searching"},
        {"type": "server_tool_use", "id": "srv-1", "name": "web_search", "input": {"query": "Python docs"}},
    ]
    results = [
        {"type": "web_search_tool_result", "tool_use_id": "srv-1", "content": [
            {"type": "web_search_result", "url": "https://docs.python.org/", "title": "Python", "encrypted_content": "opaque-result"},
        ]},
        {"type": "text", "text": "answer", "citations": [{"type": "web_search_result_location", "url": "https://docs.python.org/"}]},
    ]
    responses = [LlmResponse(stop_reason="pause_turn", content=blocks), LlmResponse(stop_reason="end_turn", content=results)]
    requests: list[list[dict[str, object]]] = []

    # 保存每次实际请求，避免上下文后续追加造成引用污染
    async def chat(**kwargs: object) -> LlmResponse:
        requests.append(deepcopy(kwargs["messages"]))
        return responses[len(requests) - 1]

    provider = AsyncMock()
    provider.chat.side_effect = chat
    events = []
    bus = EventBus()

    # 收集完成事件以核对网络与历史消费者看到同一份内容
    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    context = ExecutionContext(run_id="search", goal="goal", max_steps=3)
    registry = ToolRegistry()
    registry.register_server_tool({"type": "web_search_20250305", "name": "web_search"})
    await AgentLoop(provider, registry, bus).run(context)

    assert context.status == "success"
    assert requests[1][-1] == {"role": "assistant", "content": blocks}
    assert context.messages[-1]["content"] == results
    completed = [event for event in events if event.type == "llm.response.completed"]
    assert completed[0].content == blocks
    assert completed[0].stop_reason == "pause_turn"
    assert not any(event.type.startswith("tool.call_") for event in events)


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "pause_turn", "model_context_window_exceeded"])
# 功能：拒绝或未完成的摘要不能覆盖有效会话上下文
# 设计：返回非空但未完成的摘要，检查压缩结果为空且历史保持原样
async def test_incomplete_summary_does_not_replace_history(tmp_path: Path, stop_reason: str) -> None:
    context = ExecutionContext(run_id="compact", goal="original", max_steps=3)
    before = deepcopy(context.messages)
    provider = AsyncMock()
    provider.chat.return_value = LlmResponse(stop_reason=stop_reason, text="partial summary")

    result = await Compactor(EventBus(), tmp_path, "session").compact(context, provider)

    assert result is None
    assert context.messages == before
    assert context.summary_messages is None


# 功能：服务端搜索的查询和来源进入压缩摘要输入
# 设计：只在搜索块中放置独有网址和标题，确认压缩未静默丢弃结构化证据
async def test_compaction_keeps_server_search_evidence(tmp_path: Path) -> None:
    provider = AsyncMock()
    provider.chat.return_value = LlmResponse(stop_reason="end_turn", text="summary")
    messages = [{"role": "assistant", "content": [
        {"type": "server_tool_use", "id": "srv-1", "name": "web_search", "input": {"query": "distinct query"}},
        {"type": "web_search_tool_result", "tool_use_id": "srv-1", "content": [
            {"type": "web_search_result", "title": "Unique evidence", "url": "https://example.org/evidence", "encrypted_content": "do-not-summarize-ciphertext"},
        ]},
    ]}]

    await Compactor(EventBus(), tmp_path, "session").compact_messages(messages, provider)

    request = json.dumps(provider.chat.call_args.kwargs["messages"])
    assert "distinct query" in request
    assert "Unique evidence" in request
    assert "https://example.org/evidence" in request
    assert "do-not-summarize-ciphertext" not in request


# 功能：完整 trace 保留响应块和元信息，并转发服务端搜索能力
# 设计：从实际 JSONL 文件读取响应记录，避免仅检查内部对象掩盖序列化遗漏
async def test_trace_preserves_content_metadata_and_capability(tmp_path: Path) -> None:
    content = [{"type": "thinking", "thinking": "brief", "signature": "opaque"}, {"type": "text", "text": "answer"}]
    inner = AsyncMock()
    inner.server_search_supported = True
    inner.chat.return_value = LlmResponse(stop_reason="end_turn", content=content, metadata={"id": "msg-1"})
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path)
    await writer.start()
    provider = TracingProvider(inner, writer)

    await provider.chat([], [], EventBus(), "trace")
    await writer.stop()

    response = [json.loads(line) for line in path.read_text().splitlines()][-1]
    assert response["data"]["content"] == content
    assert response["data"]["metadata"]["id"] == "msg-1"
    assert provider.server_search_supported is True


# 功能：混合本地工具和未完成服务器搜索时暂缓压缩，保证下一请求仍能执行搜索
# 设计：超过压缩阈值但保留待执行搜索，直到第二轮收到配对结果才完成
async def test_pending_server_search_prevents_compaction() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    registry.register_server_tool({"type": "web_search_20250305", "name": "web_search"})
    content = [
        {"type": "server_tool_use", "id": "srv-1", "name": "web_search", "input": {"query": "Python"}},
        {"type": "tool_use", "id": "local-1", "name": "echo", "input": {"msg": "hello"}},
    ]
    provider = AsyncMock()
    provider.chat.side_effect = [
        LlmResponse(stop_reason="tool_use", content=content, usage=UsageStats(190000, 20, context_pct=0.95)),
        LlmResponse(stop_reason="end_turn", content=[
            {"type": "web_search_tool_result", "tool_use_id": "srv-1", "content": []},
            {"type": "text", "text": "done"},
        ]),
    ]
    compactor = AsyncMock(spec=Compactor)
    context = ExecutionContext(run_id="mixed-search", goal="goal", max_steps=3)

    await AgentLoop(provider, registry, EventBus(), compactor=compactor).run(context)

    compactor.compact.assert_not_awaited()
    assert context.status == "success"
    assert context.messages[1]["content"] == content


# 功能：服务器搜索暂停后撤销权限会明确结束，不能发送缺少必需工具的续跑请求
# 设计：首轮允许原生搜索，响应完成事件触发撤权，检查后端只被调用一次
async def test_paused_search_stops_when_permission_revoked() -> None:
    manager = PermissionManager({"web_search": ToolPolicy(PermissionDecision.DENY)})
    manager.set_session_mode("session", "full_access")
    provider = AsyncMock()
    provider.server_search_supported = True
    provider.chat.return_value = LlmResponse(stop_reason="pause_turn", content=[
        {"type": "server_tool_use", "id": "srv-1", "name": "web_search", "input": {"query": "Python"}},
    ])
    registry = ToolRegistry()
    register_web_search(registry, provider)
    bus = EventBus()

    # 在下一轮请求之前恢复默认禁止搜索策略
    async def revoke(event: object) -> None:
        if event.type == "llm.response.completed":
            manager.set_session_mode("session", "ask")

    bus.subscribe(revoke)
    context = ExecutionContext(run_id="revoked", goal="goal", max_steps=3)
    await AgentLoop(provider, registry, bus, permission_manager=manager, session_id="session").run(context)

    assert provider.chat.await_count == 1
    assert context.reason == "server_tool_unavailable"


# 功能：运行中放开搜索权限后，下一个请求获得原生工具而不需重建整个运行
# 设计：通过真实注册器先装配被禁用的搜索，执行一次允许的本地工具后改为完整访问
async def test_search_permission_is_evaluated_on_each_request() -> None:
    manager = PermissionManager({"web_search": ToolPolicy(PermissionDecision.DENY), "echo": ToolPolicy(PermissionDecision.ALLOW)})
    provider = AsyncMock()
    provider.server_search_supported = True
    provider.chat.side_effect = [
        LlmResponse(stop_reason="tool_use", tool_calls=[ToolCallBlock("echo-1", "echo", {"msg": "hi"})]),
        LlmResponse(stop_reason="end_turn", text="done"),
    ]
    registry = ToolRegistry()
    registry.register(_EchoTool())
    register_web_search(registry, provider)
    bus = EventBus()

    # 首次本地步骤完成后模拟界面切换权限模式
    async def allow(event: object) -> None:
        if event.type == "step.finished":
            manager.set_session_mode("session", "full_access")

    bus.subscribe(allow)
    context = ExecutionContext(run_id="permission", goal="goal", max_steps=3)
    await AgentLoop(provider, registry, bus, permission_manager=manager, session_id="session").run(context)

    first, second = [call.kwargs["tool_schemas"] for call in provider.chat.call_args_list]
    assert "web_search" not in {tool["name"] for tool in first}
    assert [tool for tool in second if tool["name"] == "web_search"] == [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
    ]


# 功能：手动压缩也不持久化没有模型思考内容的人工助手确认语
# 设计：走真实会话管理和摘要存储路径，断言下次发送模型的上下文只有用户摘要
async def test_manual_compaction_avoids_synthetic_assistant(tmp_path: Path) -> None:
    provider = AsyncMock()
    provider.chat.return_value = LlmResponse(stop_reason="end_turn", text="summary")
    manager = manager_for(tmp_path, provider)
    session = await manager.create("chat")
    await manager.send_message(session.id, "hello")

    await manager.compact(session.id)

    assert manager._store.read_messages(session.id) == [{"role": "user", "content": "summary"}]
