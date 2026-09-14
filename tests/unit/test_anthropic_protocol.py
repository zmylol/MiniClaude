from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import anthropic
import httpx
import pytest

from mini_claude.core.bus.events import LlmTokenEvent, LlmUsageEvent
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.provider import AnthropicProvider
from mini_claude.core.llm.types import LlmResponse, ToolCallBlock


# 将离线协议事件编码为真实 SDK 接收的 SSE 字节流
def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


# 生成包含消息边界、块边界和最终计量的完整协议事件
def _events(
    blocks: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    stop_reason: str | None = "end_turn",
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [{
        "type": "message_start", "message": {
            "id": "msg_ds", "type": "message", "role": "assistant", "model": "deepseek-test",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 20, "output_tokens": 0, "cache_hit_tokens": 7},
            "provider_extension": {"request_tag": "roundtrip"},
        },
    }]
    for index, (block, deltas) in enumerate(blocks):
        events.append({"type": "content_block_start", "index": index, "content_block": block})
        events.extend({"type": "content_block_delta", "index": index, "delta": delta}
                      for delta in deltas)
        events.append({"type": "content_block_stop", "index": index})
    events.extend([
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
         "usage": {"output_tokens": 8, "output_tokens_details": {"reasoning_tokens": 3}}},
        {"type": "message_stop"},
    ])
    return events


# 为每次请求依次提供离线响应，同时记录真实 SDK 发送的请求体
def _provider(
    payloads: list[bytes], base_url: str = "https://api.deepseek.com/anthropic",
) -> tuple[AnthropicProvider, anthropic.AsyncAnthropic, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

    # 以请求序号选择响应并保留请求内容用于验证后续回传
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        payload = payloads[min(len(requests) - 1, len(payloads) - 1)]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload)

    client = anthropic.AsyncAnthropic(
        api_key="offline-test", base_url=base_url, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    return AnthropicProvider("deepseek-test", client), client, requests


# 功能：完整响应保留原始块顺序、加密思考、搜索结果、引用和兼容端点新增字段
# 设计：直接走真实 SDK 的 SSE 聚合路径并再次发送历史，检测选择性提取、默认字段填充和内部缓冲区泄漏
async def test_sdk_stream_preserves_content_and_metadata_roundtrip() -> None:
    citation = {"type": "web_search_result_location", "cited_text": "source",
                "url": "https://example.com", "title": "Source", "encrypted_index": "enc"}
    blocks = [
        ({"type": "text", "text": "before ", "provider_tag": "keep"}, []),
        ({"type": "thinking", "thinking": "", "signature": ""},
         [{"type": "signature_delta", "signature": "opaque-signature"}]),
        ({"type": "redacted_thinking", "data": "encrypted-thinking"}, []),
        ({"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {}},
         [{"type": "input_json_delta", "partial_json": '{"query":"sdk"}'}]),
        ({"type": "web_search_tool_result", "tool_use_id": "srv_1", "content": [
            {"type": "web_search_result", "title": "Source", "url": "https://example.com",
             "encrypted_content": "opaque-result"},
        ]}, []),
        ({"type": "text", "text": ""}, [{"type": "text_delta", "text": "after"},
                                            {"type": "citations_delta", "citation": citation}]),
        ({"type": "tool_use", "id": "local_1", "name": "read_file", "input": {}},
         [{"type": "input_json_delta", "partial_json": '{"path":"README.md"}'}]),
        ({"type": "deepseek_future_block", "payload": {"opaque": [1, 2]}}, []),
    ]
    expected = [deepcopy(block) for block, _ in blocks]
    expected[1]["signature"] = "opaque-signature"
    expected[3]["input"] = {"query": "sdk"}
    expected[5].update(text="after", citations=[citation])
    expected[6]["input"] = {"path": "README.md"}
    provider, client, requests = _provider([_sse(_events(blocks, "tool_use"))])
    async with client:
        result = await provider.chat([], [], EventBus(), "preserve")
        assert result.content == expected
        assert result.text == "before after"
        assert result.tool_calls == [ToolCallBlock("local_1", "read_file", {"path": "README.md"})]
        assert result.thinking_blocks == expected[1:3]
        assert result.metadata["id"] == "msg_ds"
        assert result.metadata["provider_extension"] == {"request_tag": "roundtrip"}
        assert result.metadata["usage"] == {
            "input_tokens": 20, "output_tokens": 8, "cache_hit_tokens": 7,
            "output_tokens_details": {"reasoning_tokens": 3},
        }
        await provider.chat([{"role": "assistant", "content": result.assistant_content()}],
                            [], EventBus(), "roundtrip")
    assert requests[1]["messages"][0]["content"] == expected


@pytest.mark.parametrize("missing", ["message_stop", "block_stop", "stop_reason"])
# 功能：正常关闭连接也不能把缺少协议结束标志的响应当作成功
# 设计：真实 SDK 能返回这些半成品消息，因此断言 provider 重试三次并且不发布成功用量
async def test_incomplete_sse_is_retried_then_rejected(
    monkeypatch: pytest.MonkeyPatch, missing: str,
) -> None:
    events = _events([({"type": "text", "text": ""},
                       [{"type": "text_delta", "text": "INCOMPLETE"}])])
    if missing == "message_stop":
        events.pop()
    elif missing == "block_stop":
        events = [event for event in events if event["type"] != "content_block_stop"]
    else:
        events[-2]["delta"]["stop_reason"] = None
    monkeypatch.setattr("mini_claude.core.llm.provider._RETRY_BACKOFF_S", (0, 0, 0))
    provider, client, requests = _provider([_sse(events)])
    bus = EventBus()
    seen: list[Any] = []

    # 保存事件以确认未完成响应不会被记成成功用量
    async def collect(event: Any) -> None:
        seen.append(event)

    bus.subscribe(collect)
    async with client:
        with pytest.raises(RuntimeError, match="incomplete"):
            await provider.chat([], [], bus, "incomplete")
    assert len(requests) == 3
    assert not any(isinstance(event, LlmUsageEvent) for event in seen)


# 功能：半截响应重试成功时仅返回最终完整正文且不重复发布临时 token
# 设计：首个 SSE 没有结束事件、第二个完整结束，覆盖无网络异常的实际断流恢复路径
async def test_normal_eof_retry_returns_only_complete_response(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = _events([({"type": "text", "text": ""},
                        [{"type": "text_delta", "text": "PARTIAL"}])])[:-1]
    complete = _events([({"type": "text", "text": ""},
                         [{"type": "text_delta", "text": "COMPLETE"}])])
    monkeypatch.setattr("mini_claude.core.llm.provider._RETRY_BACKOFF_S", (0, 0, 0))
    provider, client, requests = _provider([_sse(partial), _sse(complete)])
    bus = EventBus()
    tokens: list[str] = []

    # 只收集临时文本事件以检测重试带来的重复内容
    async def collect(event: Any) -> None:
        if isinstance(event, LlmTokenEvent):
            tokens.append(event.token)

    bus.subscribe(collect)
    async with client:
        result = await provider.chat([], [], bus, "retry")
    assert len(requests) == 2
    assert result.text == "COMPLETE"
    assert tokens == ["PARTIAL"]


# 功能：原始有序内容是唯一事实来源且回传副本不能污染模型响应
# 设计：传入故意矛盾的便利字段及空内容，验证派生语义和深拷贝边界
def test_response_derives_fields_from_content_and_isolates_history() -> None:
    content = [{"type": "text", "text": "actual"},
               {"type": "tool_use", "id": "tool_1", "name": "read_file", "input": {"path": "a"}}]
    response = LlmResponse(stop_reason="tool_use", content=content, text="stale")
    assert response.text == "actual"
    assert response.tool_calls == [ToolCallBlock("tool_1", "read_file", {"path": "a"})]
    history = response.assistant_content()
    history[1]["input"]["path"] = "changed"
    assert response.content == content
    assert LlmResponse(stop_reason="end_turn", content=[], text="stale").text == ""


@pytest.mark.parametrize(("base_url", "supported"), [
    ("https://api.deepseek.com/anthropic", True),
    ("https://api.deepseek.com/anthropic/v1/", True),
    ("https://api.deepseek.com/v1", False),
    ("https://api.deepseek.com.proxy.example/anthropic", False),
    ("http://api.deepseek.com/anthropic", False),
    ("https://api.anthropic.com", False),
])
# 功能：只有官方 DeepSeek Anthropic 端点自动声明服务端搜索支持
# 设计：分别覆盖兼容路径、OpenAI 路径、相似恶意域名和自定义端点，避免依据模型名称猜测能力
def test_server_search_capability_uses_actual_endpoint(base_url: str, supported: bool) -> None:
    provider = AnthropicProvider("deepseek-test", type("Client", (), {"base_url": base_url})())
    assert provider.server_search_supported is supported


# 功能：服务端搜索工具定义原样送达兼容端点
# 设计：用真实 SDK 请求体检查原生 schema 不被额外添加客户端缓存控制字段
async def test_native_search_schema_is_forwarded_without_cache_control() -> None:
    schema = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
    provider, client, requests = _provider([_sse(_events([]))])
    async with client:
        await provider.chat([], [schema], EventBus(), "search-schema")
    assert requests[0]["tools"] == [schema]
