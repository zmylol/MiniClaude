from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from mini_claude.core.config import MiniConfig
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from mini_claude.core.runner import AgentRunner
from mini_claude.core.session.manager import SessionManager
from mini_claude.core.session.store import SessionStore
from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
# 避免单独运行压力用例时读取真实上下文，文件操作均发生在测试目录
def isolated_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mini_claude.core.runner.load_context_file", lambda path: "")


class ScriptedProvider:
    # 保存普通模型输入与摘要输入，按剧本触发模型成功、摘要失败或取消
    def __init__(
        self, responses: list[LlmResponse], summaries: list[str | BaseException] | None = None,
    ) -> None:
        self.responses = responses
        self.summaries = list(summaries or [])
        self.inputs: list[Any] = []
        self.summary_inputs: list[Any] = []

    # 仅替换外部模型，保留真实运行器、压缩器和磁盘存储调用链
    async def chat(self, *, messages: Any, run_id: str, **kwargs: Any) -> LlmResponse:
        if run_id == "compact":
            self.summary_inputs.append(copy.deepcopy(messages))
            summary = self.summaries.pop(0)
            if isinstance(summary, BaseException):
                raise summary
            return LlmResponse(stop_reason="end_turn", text=summary)
        self.inputs.append(copy.deepcopy(messages))
        return self.responses[len(self.inputs) - 1]


# 功能：消息中的 Unicode 分隔符不会被当成 JSONL 记录边界，也不会移动摘要覆盖位置
# 设计：保存含真实字符的消息并跨检查点重启读取，覆盖从文档粘贴的换行及段落分隔符
@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
def test_unicode_separators_preserve_physical_history_records(
    tmp_path: Path, separator: str,
) -> None:
    store = SessionStore(tmp_path)
    sid = "sess-unicode"
    content = f"before{separator}after"
    store.append_message(sid, "user", content)
    assert store.history_position(sid) == 1
    assert store.read_history(sid) == [{"role": "user", "content": content}]
    store.write_compacted(sid, [{"role": "user", "content": "SUMMARY"}], covered_through=1)
    store.append_message(sid, "assistant", content)
    restarted = SessionStore(tmp_path)
    assert restarted.history_position(sid) == 2
    assert restarted.read_messages(sid) == [
        {"role": "user", "content": "SUMMARY"}, {"role": "assistant", "content": content},
    ]


# 功能：日志末尾无换行、半条 JSON 或半个 UTF-8 字符都不能吞掉重启后的新请求
# 设计：直接写入中断留下的原始字节，再走真实 manager 追加和模型调用，检查可用历史仍可读取
@pytest.mark.parametrize("tail", [
    b'{"role":"assistant","content":"partial',
    b'{"role":"assistant","content":"\xe4\xb8',
    b'{"role":"assistant","content":"COMPLETE"}',
])
async def test_restart_after_unterminated_record_keeps_new_request(
    tmp_path: Path, tail: bytes,
) -> None:
    provider = ScriptedProvider([LlmResponse(stop_reason="end_turn", text="RECOVERED")])
    manager = make_manager(tmp_path, provider)
    session = await manager.create("chat")
    manager._store.append_message(session.id, "user", "ORIGINAL")
    thread = manager._store.session_dir(session.id) / "thread.jsonl"
    with thread.open("ab") as stream:
        stream.write(tail)
    restarted = make_manager(tmp_path, provider)
    await restarted.send_message(session.id, "NEW REQUEST")
    assert provider.inputs[0][-1] == {"role": "user", "content": "NEW REQUEST"}
    assert provider.inputs[0][0] == {"role": "user", "content": "ORIGINAL"}
    assert restarted._store.history_position(session.id) == 4
    history = await restarted.get_history(session.id)
    assert history[-2]["content"] == "NEW REQUEST"
    assert history[-1]["content"] == [{"type": "text", "text": "RECOVERED"}]
    assert len(history) == (4 if tail.endswith(b'}') else 3)


# 创建有摘要阈值的真实会话管理器，关闭网络工具以保证完全离线
def make_manager(path: Path, provider: ScriptedProvider, bus: EventBus | None = None) -> SessionManager:
    config = MiniConfig()
    config.network.enabled = False
    config.compaction.auto_threshold = 0.8
    event_bus = bus or EventBus()
    return SessionManager(
        SessionStore(path),
        lambda: AgentRunner(config, provider=provider, bus=event_bus, runs_dir=path / "runs"),
        event_bus, provider=provider, project_path=path,
    )


# 构造会触发自动压缩的同一步工具批次
def tool_batch(*ids: str, name: str = "read_file") -> LlmResponse:
    return LlmResponse(
        stop_reason="tool_use", text="BATCH " + ",".join(ids),
        tool_calls=[ToolCallBlock(id=tid, name=name, input={"path": tid}) for tid in ids],
        usage=UsageStats(input_tokens=100, output_tokens=10, context_pct=0.9),
    )


# 提取历史中的工具结果，便于检查真实磁盘记录的配对和内容
def results_in(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        block["tool_use_id"]: block
        for message in messages if isinstance(message["content"], list)
        for block in message["content"] if block.get("type") == "tool_result"
    }


# 功能：交错的摘要成功、失败与空摘要不会漏存并行工具结果或重复旧消息
# 设计：五个真实文件读取批次穿过同轮多次压缩，再重建 manager 检查最终摘要和全部原始记录
async def test_mixed_compaction_outcomes_keep_all_parallel_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    ids = [f"file-{step}-{index}" for step in range(5) for index in range(2)]
    for tid in ids:
        (tmp_path / tid).write_text("VALUE " + tid, encoding="utf-8")
    provider = ScriptedProvider(
        [tool_batch(*ids[index:index + 2]) for index in range(0, len(ids), 2)]
        + [LlmResponse(stop_reason="end_turn", text="COMPLETE")],
        ["FIRST SUMMARY", RuntimeError("summary unavailable"), "", "FOURTH SUMMARY", "LAST SUMMARY"],
    )
    manager = make_manager(tmp_path / "sessions", provider)
    session = await manager.create("chat")
    await manager.send_message(session.id, "READ ALL FILES")

    history = await manager.get_history(session.id)
    results = results_in(history)
    assert len(history) == 12
    assert list(results) == ids
    assert [block["tool_use_id"] for message in history if isinstance(message["content"], list)
            for block in message["content"] if block.get("type") == "tool_result"] == ids
    assert all("VALUE " + tid in result["content"] for tid, result in results.items())
    assert all(not result.get("is_error") for result in results.values())
    assert "SUMMARY" not in json.dumps(history)
    assert "VALUE file-1-0" in json.dumps(provider.inputs[3])

    restarted_provider = ScriptedProvider([LlmResponse(stop_reason="end_turn", text="NEXT DONE")])
    restarted = make_manager(tmp_path / "sessions", restarted_provider)
    await restarted.send_message(session.id, "NEXT REQUEST")
    assert restarted_provider.inputs[0] == [
        {"role": "user", "content": "LAST SUMMARY"},
        {"role": "assistant", "content": "Understood, I'll continue from this summary."},
        {"role": "assistant", "content": [{"type": "text", "text": "COMPLETE"}]},
        {"role": "user", "content": "NEXT REQUEST"},
    ]
    assert (await restarted.get_history(session.id))[:len(history)] == history


class CancellableTool(BaseTool):
    name = "controlled"
    description = "Test tool with observable cancellation"
    input_schema: dict[str, object] = {"type": "object", "properties": {"path": {"type": "string"}}}

    # 使用事件精确控制挂起点，避免测试依赖任意延迟或机器速度
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleaned = asyncio.Event()

    # 普通调用立即完成，慢调用一直等待取消并执行资源清理
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        if params["path"] != "slow":
            return ToolResult(content="VALUE " + str(params["path"]))
        self.started.set()
        try:
            await asyncio.Future()
        finally:
            self.cleaned.set()
        raise AssertionError("unreachable")


# 功能：压缩后取消并行批次仍保留已经完成的结果，并为被取消调用补齐错误结果
# 设计：等待真实工具完成事件后取消会话运行，重启读取摘要后的完整配对记录及清理事件
async def test_cancel_parallel_batch_after_compaction_keeps_completed_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = CancellableTool()
    registry = ToolRegistry()
    registry.register(tool)
    monkeypatch.setattr(AgentRunner, "_build_registry", lambda *args, **kwargs: registry)
    provider = ScriptedProvider(
        [tool_batch("seed", name=tool.name), tool_batch("fast", "slow", name=tool.name)],
        ["SEED SUMMARY"],
    )
    bus = EventBus()
    completed = asyncio.Event()

    # 在工具调用任务返回前最后一个事件处标记快工具完成
    async def observe(event: Any) -> None:
        if event.type == "tool.call_finished" and event.tool_use_id == "fast":
            completed.set()

    bus.subscribe(observe)
    manager = make_manager(tmp_path, provider, bus)
    session = await manager.create("chat")
    task = asyncio.create_task(manager.send_message(session.id, "START"))
    try:
        await asyncio.wait_for(asyncio.gather(tool.started.wait(), completed.wait()), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert tool.cleaned.is_set()
    history = SessionStore(tmp_path).read_history(session.id)
    assert len(history) == 5
    results = results_in(history)
    assert set(results) == {"seed", "fast", "slow"}
    assert results["fast"]["content"] == "VALUE fast"
    assert not results["fast"].get("is_error")
    assert results["slow"]["is_error"] is True
    assert "interrupted" in results["slow"]["content"]
    model_messages = SessionStore(tmp_path).read_messages(session.id)
    assert model_messages[0]["content"] == "SEED SUMMARY"
    assert set(results_in(model_messages)) == {"fast", "slow"}


# 功能：摘要模型在第二次压缩时取消不会丢失已执行工具，也不会覆盖上次摘要
# 设计：在摘要调用内抛取消以覆盖步骤尚未 flush 的窗口，重启后应是旧摘要加新工具记录
async def test_cancellation_inside_summary_keeps_previous_checkpoint_and_new_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "one").write_text("ONE", encoding="utf-8")
    (tmp_path / "two").write_text("TWO", encoding="utf-8")
    provider = ScriptedProvider(
        [tool_batch("one"), tool_batch("two")], ["COMMITTED SUMMARY", asyncio.CancelledError()],
    )
    manager = make_manager(tmp_path / "sessions", provider)
    session = await manager.create("chat")
    with pytest.raises(asyncio.CancelledError):
        await manager.send_message(session.id, "READ")
    restarted = SessionStore(tmp_path / "sessions")
    assert len(restarted.read_history(session.id)) == 5
    model_messages = restarted.read_messages(session.id)
    assert model_messages[0]["content"] == "COMMITTED SUMMARY"
    assert list(results_in(model_messages)) == ["two"]
    assert "TWO" in results_in(model_messages)["two"]["content"]


# 功能：写入一半步骤后发生暂时磁盘错误，最终补存不会重复已提交的 assistant 消息
# 设计：只在工具结果首次写入前失败，允许运行器最终清理恢复写入，并从新 store 检查配对
async def test_partial_step_write_recovers_without_duplicate_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider([tool_batch("missing")], ["PENDING SUMMARY"])
    manager = make_manager(tmp_path, provider)
    session = await manager.create("chat")
    original_append = manager._store.append_message
    failed = False

    # 模拟 assistant 已保存、tool_result 尚未保存时一次磁盘错误
    def append_with_failure(sid: str, role: str, content: Any, run_id: str | None = None) -> None:
        nonlocal failed
        if role == "user" and isinstance(content, list) and not failed:
            failed = True
            raise OSError("temporary disk failure")
        original_append(sid, role, content, run_id)

    monkeypatch.setattr(manager._store, "append_message", append_with_failure)
    await manager.send_message(session.id, "GOAL")
    store = SessionStore(tmp_path)
    assert failed
    assert len(store.read_history(session.id)) == 3
    assert list(results_in(store.read_history(session.id))) == ["missing"]
    assert store.read_messages(session.id)[0]["content"] == "PENDING SUMMARY"
    assert manager._outcomes[session.id] == ("failed", "persistence_error")


# 功能：替换摘要文件失败保留旧摘要及完整历史尾部，且不遗留临时文件
# 设计：在真实原子替换处注入失败，重建 store 验证恢复路径而不模拟序列化或文件写入
def test_checkpoint_replace_failure_preserves_last_durable_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    sid = "sess-checkpoint"
    store.append_message(sid, "user", "OLD REQUEST")
    store.write_compacted(sid, [{"role": "user", "content": "OLD SUMMARY"}])
    store.append_message(sid, "assistant", "NEW ANSWER")
    checkpoint = store.session_dir(sid) / "context.json"
    before = checkpoint.read_bytes()
    original_replace = Path.replace

    # 拒绝最终替换，使已 fsync 的临时摘要无法成为有效检查点
    def replace_with_failure(path: Path, target: Any) -> Path:
        if Path(target) == checkpoint:
            raise OSError("replace unavailable")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace_with_failure)
    with pytest.raises(OSError, match="replace unavailable"):
        store.write_compacted(sid, [{"role": "user", "content": "NEW SUMMARY"}])
    assert checkpoint.read_bytes() == before
    assert not list(store.session_dir(sid).glob("context-*"))
    assert SessionStore(tmp_path).read_messages(sid) == [
        {"role": "user", "content": "OLD SUMMARY"},
        {"role": "assistant", "content": "NEW ANSWER"},
    ]


# 功能：历史中非对象 JSON 和语法坏行都不能使重启失败或改变摘要覆盖位置
# 设计：将各种坏行插在摘要前后，以物理行号建立检查点并检查新尾部不会被误判为已覆盖
@pytest.mark.parametrize("bad_line", ["{broken", "null", "[]", '"unexpected string"'])
def test_corrupt_history_rows_preserve_checkpoint_positions(tmp_path: Path, bad_line: str) -> None:
    store = SessionStore(tmp_path)
    sid = "sess-damaged"
    store.append_message(sid, "user", "SUMMARIZED REQUEST")
    thread = store.session_dir(sid) / "thread.jsonl"
    with thread.open("a", encoding="utf-8") as stream:
        stream.write(bad_line + "\n\n")
    store.write_compacted(sid, [{"role": "user", "content": "SUMMARY"}], covered_through=3)
    with thread.open("a", encoding="utf-8") as stream:
        stream.write(bad_line + "\n")
    store.append_message(sid, "assistant", "AFTER CHECKPOINT")

    restarted = SessionStore(tmp_path)
    assert restarted.history_position(sid) == 5
    assert restarted.read_history(sid) == [
        {"role": "user", "content": "SUMMARIZED REQUEST"},
        {"role": "assistant", "content": "AFTER CHECKPOINT"},
    ]
    assert restarted.read_messages(sid) == [
        {"role": "user", "content": "SUMMARY"},
        {"role": "assistant", "content": "AFTER CHECKPOINT"},
    ]


# 功能：损坏或引用未提交历史的摘要检查点应回退原始记录，并接受重启后的下一轮请求
# 设计：对真实 context.json 注入语法错误、无效消息与超前位置，检验模型实际输入而非仅解析函数
@pytest.mark.parametrize("checkpoint", [
    "{incomplete",
    '{"covered_through": 2, "messages": [null]}',
    '{"covered_through": 99, "messages": [{"role": "user", "content": "WRONG SUMMARY"}]}',
])
async def test_invalid_checkpoint_falls_back_to_raw_history(
    tmp_path: Path, checkpoint: str,
) -> None:
    provider = ScriptedProvider([LlmResponse(stop_reason="end_turn", text="NEXT ANSWER")])
    manager = make_manager(tmp_path, provider)
    session = await manager.create("chat")
    store = manager._store
    store.append_message(session.id, "user", "ORIGINAL REQUEST")
    store.append_message(session.id, "assistant", "ORIGINAL ANSWER")
    raw_before = (store.session_dir(session.id) / "thread.jsonl").read_bytes()
    (store.session_dir(session.id) / "context.json").write_text(checkpoint, encoding="utf-8")

    restarted = make_manager(tmp_path, provider)
    await restarted.send_message(session.id, "NEXT REQUEST")
    assert provider.inputs[0] == [
        {"role": "user", "content": "ORIGINAL REQUEST"},
        {"role": "assistant", "content": "ORIGINAL ANSWER"},
        {"role": "user", "content": "NEXT REQUEST"},
    ]
    assert (store.session_dir(session.id) / "thread.jsonl").read_bytes().startswith(raw_before)
    assert len(await restarted.get_history(session.id)) == 4


# 功能：进程在工具调用落盘后中断，重启后的新用户请求仍必须真正送入模型
# 设计：直接构造中断时的合法历史前缀，再调用真实 manager，避免只检查界面可见历史
async def test_restart_after_unfinished_tool_does_not_hide_new_request(tmp_path: Path) -> None:
    provider = ScriptedProvider([LlmResponse(stop_reason="end_turn", text="NEW ANSWER")])
    manager = make_manager(tmp_path, provider)
    session = await manager.create("chat")
    store = manager._store
    store.append_message(session.id, "user", "OLD REQUEST")
    store.append_message(session.id, "assistant", [
        {"type": "text", "text": "READING OLD FILE"},
        {"type": "tool_use", "id": "unfinished", "name": "read_file", "input": {"path": "old"}},
    ])
    restarted = make_manager(tmp_path, provider)
    await restarted.send_message(session.id, "NEW REQUEST AFTER RESTART")
    assert provider.inputs[0][-1] == {"role": "user", "content": "NEW REQUEST AFTER RESTART"}
    history = await restarted.get_history(session.id)
    assert len(history) == 4
    assert "unfinished" in json.dumps(history)
    assert history[-2]["content"] == "NEW REQUEST AFTER RESTART"


# 功能：部分工具结果落盘后的重启保留成功结果，仅补一次缺失结果且不改原始历史
# 设计：摘要后存入两个调用和一个真实结果，重复模型读取并开启新轮，验证恢复投影幂等且请求可见
async def test_restart_with_partial_tool_results_repairs_only_missing_result(tmp_path: Path) -> None:
    provider = ScriptedProvider([LlmResponse(stop_reason="end_turn", text="RECOVERED ANSWER")])
    manager = make_manager(tmp_path, provider)
    session = await manager.create("chat")
    store = manager._store
    store.append_message(session.id, "user", "ORIGINAL REQUEST")
    store.write_compacted(session.id, [{"role": "user", "content": "PREVIOUS SUMMARY"}])
    store.append_message(session.id, "assistant", [
        {"type": "text", "text": "READING TWO FILES"},
        {"type": "tool_use", "id": "done", "name": "read_file", "input": {"path": "a"}},
        {"type": "tool_use", "id": "unknown", "name": "read_file", "input": {"path": "b"}},
    ])
    saved_result = {"type": "tool_result", "tool_use_id": "done", "content": "SAVED FILE CONTENT"}
    store.append_message(session.id, "user", [saved_result])
    thread = store.session_dir(session.id) / "thread.jsonl"
    raw_before = thread.read_bytes()
    history_before = store.read_history(session.id)

    restarted_store = SessionStore(tmp_path)
    first_read = restarted_store.read_messages(session.id)
    assert restarted_store.read_messages(session.id) == first_read
    assert thread.read_bytes() == raw_before
    repaired = results_in(first_read)
    assert repaired["done"] == saved_result
    assert repaired["unknown"]["is_error"] is True
    assert "READING TWO FILES" in json.dumps(first_read)
    assert first_read[0]["content"] == "PREVIOUS SUMMARY"
    all_results = [
        block for message in first_read if isinstance(message["content"], list)
        for block in message["content"] if block.get("type") == "tool_result"
    ]
    assert len(all_results) == 2

    restarted = make_manager(tmp_path, provider)
    await restarted.send_message(session.id, "CHECK STATUS BEFORE RETRYING")
    assert provider.inputs[0][-1] == {"role": "user", "content": "CHECK STATUS BEFORE RETRYING"}
    assert results_in(provider.inputs[0]) == repaired
    history = await restarted.get_history(session.id)
    assert history[:len(history_before)] == history_before
    assert len(history) == len(history_before) + 2
    assert set(results_in(history)) == {"done"}
