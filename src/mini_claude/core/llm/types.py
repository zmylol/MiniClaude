from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field


@dataclass
class UsageStats:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    context_pct: float = 0.0


@dataclass
class ToolCallBlock:
    id: str
    name: str
    input: dict[str, object]


@dataclass
class LlmResponse:
    stop_reason: str
    tool_calls: list[ToolCallBlock] = field(default_factory=list)
    text: str = ""
    usage: UsageStats | None = None
    thinking_blocks: list[dict[str, object]] = field(default_factory=list)
    content: list[dict[str, object]] | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    # 从原始有序内容派生便利字段，避免历史记录依赖二次重组
    def __post_init__(self) -> None:
        if self.content is None:
            return
        self.content = deepcopy(self.content)
        self.text = "".join(
            str(block.get("text", "")) for block in self.content if block.get("type") == "text"
        )
        self.tool_calls = [
            ToolCallBlock(str(block["id"]), str(block["name"]), deepcopy(inputs))
            for block in self.content
            if block.get("type") == "tool_use" and isinstance(inputs := block.get("input"), dict)
        ]
        self.thinking_blocks = [
            deepcopy(block) for block in self.content
            if block.get("type") in {"thinking", "redacted_thinking"}
        ]

    # 返回可独立修改的历史内容，并兼容尚未提供原始块的测试或其他 provider
    def assistant_content(self) -> list[dict[str, object]]:
        if self.content is not None:
            return deepcopy(self.content)
        blocks = deepcopy(self.thinking_blocks)
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        blocks.extend({"type": "tool_use", "id": call.id, "name": call.name,
                       "input": deepcopy(call.input)} for call in self.tool_calls)
        return blocks
