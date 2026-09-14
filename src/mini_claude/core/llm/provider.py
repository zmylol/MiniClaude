from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import anthropic
import httpx

from mini_claude.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.errors import IncompleteResponseError
from mini_claude.core.llm.types import LlmResponse, UsageStats

_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-opus-4-7": 200_000,
}

_MAX_STREAM_RETRIES = 3
_RETRY_BACKOFF_S = (1.0, 2.0, 4.0)
_DELTA_FIELDS = {
    "text_delta": "text", "input_json_delta": "input", "citations_delta": "citations",
    "thinking_delta": "thinking", "signature_delta": "signature",
}

log = logging.getLogger(__name__)


# 返回指定模型的最大 context window token 数
def _context_window(model: str) -> int:
    return _MODEL_CONTEXT_WINDOWS.get(model, 200_000)


_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Use the available tools to complete the user's goal. "
    "When the goal is fully achieved, respond with a final answer and do not call any more tools."
)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class AnthropicProvider:
    # 初始化 Anthropic 客户端；client 可在测试时注入以跳过 API key 检查
    def __init__(self, model: str, client: Any = None) -> None:
        if client is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            self._client: Any = anthropic.AsyncAnthropic(api_key=api_key)
        else:
            self._client = client
        self._model = model

    @property
    # 依据实际官方兼容端点声明服务端搜索能力，避免凭模型名误判中转服务
    def server_search_supported(self) -> bool:
        base_url = getattr(self._client, "base_url", None)
        if not isinstance(base_url, (str, httpx.URL)):
            return False
        url = urlparse(str(base_url))
        return (
            url.scheme == "https" and url.hostname == "api.deepseek.com"
            and url.path.rstrip("/") in {"/anthropic", "/anthropic/v1"}
        )

    # 流式调用 Anthropic API，逐 token 发布事件并返回 LlmResponse；网络中断时自动重试
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        await bus.publish(
            LlmModelSelectedEvent(run_id=run_id, model=self._model, strategy="static", ts=_now())
        )

        system_blocks: list[dict[str, object]] = [
            {
                "type": "text",
                "text": system or _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
        ]

        tools: list[dict[str, object]] = list(tool_schemas)
        if tools and "input_schema" in tools[-1]:
            last = dict(tools[-1])
            last["cache_control"] = {"type": "ephemeral"}
            tools = tools[:-1] + [last]

        kwargs: dict[str, object] = {
            "model": self._model,
            "max_tokens": 8192,
            "system": system_blocks,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        final_message: Any = None
        content: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}

        for attempt in range(1, _MAX_STREAM_RETRIES + 1):
            content = []
            metadata = {}
            changed_fields: dict[int, set[str]] = {}
            open_blocks: set[int] = set()
            message_stopped = False
            try:
                async with self._client.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        if event.type == "text" and attempt == 1:
                            # 重试时由最终完成事件覆盖临时正文，避免前端重复拼接
                            await bus.publish(LlmTokenEvent(
                                run_id=run_id, step=step, token=event.text, ts=_now(),
                            ))
                        elif event.type == "message_start":
                            metadata = event.message.model_dump(mode="json", exclude_unset=True)
                            content = metadata.pop("content", [])
                        elif event.type == "content_block_start":
                            content.append(event.content_block.model_dump(
                                mode="json", exclude_unset=True,
                            ))
                            open_blocks.add(event.index)
                            changed_fields[event.index] = set()
                        elif event.type == "content_block_delta":
                            field = _DELTA_FIELDS.get(event.delta.type)
                            if field is not None:
                                changed_fields[event.index].add(field)
                        elif event.type == "content_block_stop":
                            # 仅合入 SDK 聚合的增量字段，保留原字段并排除内部 JSON 缓冲区
                            content[event.index].update(event.content_block.model_dump(
                                mode="json", exclude_unset=True,
                                include=changed_fields[event.index],
                            ))
                            open_blocks.discard(event.index)
                        elif event.type == "message_delta":
                            metadata.update(event.delta.model_dump(mode="json", exclude_unset=True))
                            metadata.setdefault("usage", {}).update(event.usage.model_dump(
                                mode="json", exclude_unset=True,
                            ))
                        elif event.type == "message_stop":
                            message_stopped = True
                    if not message_stopped or open_blocks:
                        raise IncompleteResponseError(
                            "incomplete response: missing stream boundary",
                        )
                    final_message = await stream.get_final_message()
                    if not final_message.stop_reason:
                        raise IncompleteResponseError("incomplete response: missing stop_reason")
                break
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError,
                    IncompleteResponseError) as exc:
                if attempt == _MAX_STREAM_RETRIES:
                    log.error(
                        "stream failed after %d attempts run_id=%s step=%d: %s",
                        _MAX_STREAM_RETRIES, run_id, step, exc,
                    )
                    raise
                delay = _RETRY_BACKOFF_S[attempt - 1]
                log.warning(
                    "stream dropped (attempt %d/%d) run_id=%s step=%d: %s — retrying in %.0fs",
                    attempt, _MAX_STREAM_RETRIES, run_id, step, exc, delay,
                )
                await asyncio.sleep(delay)

        assert final_message is not None

        usage = final_message.usage
        cache_read: int = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create: int = getattr(usage, "cache_creation_input_tokens", 0) or 0
        total_input_tokens = usage.input_tokens + cache_read + cache_create
        context_pct = total_input_tokens / _context_window(self._model)

        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_create,
                context_pct=context_pct,
                ts=_now(),
            )
        )

        return LlmResponse(
            stop_reason=final_message.stop_reason,
            content=content,
            metadata=metadata,
            usage=UsageStats(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_create,
                context_pct=context_pct,
            ),
        )
