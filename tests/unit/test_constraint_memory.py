from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mini_claude.evaluation.constraint_memory import (
    Budget,
    BudgetExceeded,
    MemoryTask,
    Model,
    aggregate,
    build_memory,
    clip_utf8,
    main,
    run_case,
    score_answer,
)


# 创建包含旧决定、已确认更新和未批准建议的公开任务
def sample_task() -> MemoryTask:
    return MemoryTask.model_validate(
        {
            "id": "sample",
            "category": "proposal_not_approval",
            "query": "Choose storage for the release.",
            "fields": ["storage"],
            "episodes": [
                [
                    {
                        "id": "e1",
                        "role": "user",
                        "text": "Use sqlite for storage.",
                        "decisions": [
                            {
                                "key": "storage",
                                "status": "confirmed",
                                "supersedes": [],
                            }
                        ],
                    },
                    {
                        "id": "e2",
                        "role": "user",
                        "text": "Replace sqlite with postgres.",
                        "decisions": [
                            {
                                "key": "storage",
                                "status": "confirmed",
                                "supersedes": ["e1"],
                            }
                        ],
                    },
                    {
                        "id": "e3",
                        "role": "assistant",
                        "text": "I suggest redis storage.",
                        "decisions": [
                            {
                                "key": "storage",
                                "status": "proposed",
                                "supersedes": ["e2"],
                            }
                        ],
                    },
                ]
            ],
        }
    )


# 功能：未确认建议不能取代有效决定且恢复只包含当前版本原文
# 设计：让建议时间最新并引用有效决定，捕获按最新消息直接覆盖的错误
def test_versioned_memory_requires_confirmation() -> None:
    memory = build_memory(sample_task(), "", "versioned", 2048)
    assert "postgres" in memory
    assert "Replace sqlite with postgres." in memory
    assert "redis" not in memory
    assert '"id":"e1"' not in memory


# 功能：检索基线保留角色和原始更新信息以提供公平对照
# 设计：宽松预算下检查三条证据均可见，不能为增强组私藏版本信息
def test_retrieval_gets_same_public_metadata() -> None:
    memory = build_memory(sample_task(), "", "retrieval", 4096)
    for word in ("sqlite", "postgres", "redis", "supersedes", "assistant", "user"):
        assert word in memory


@pytest.mark.parametrize(
    "method", ["summary", "retrieval", "versioned", "no_versions", "no_evidence"]
)
# 功能：所有方法的摘要和证据总和都遵守相同字节上限
# 设计：混入多字节中文和超长摘要，捕获字符数冒充字节数及证据额外占额的问题
def test_context_budget_counts_all_payload(method: str) -> None:
    memory = build_memory(sample_task(), "约束" * 3000, method, 1024)
    assert len(memory.encode("utf-8")) <= 1024
    assert "\ufffd" not in memory


# 功能：UTF8 截断保留完整码点
# 设计：在中文字符内部截断，避免损坏提示词文本
def test_clip_utf8() -> None:
    assert clip_utf8("a中b", 3) == "a"


# 功能：错误、缺字段及空答案计入失败而不被评分器漏掉
# 设计：逐一注入语法错误、类型错误与漏答，避免模型失败被排除出分母
@pytest.mark.parametrize("answer", ["bad", "{}", "[]", '{"storage": "postgres"}'])
def test_invalid_answers_fail(answer: str) -> None:
    gold = {"storage": {"value": "postgres", "evidence_id": "e2"}}
    result = score_answer(answer, gold, sample_task())
    assert result["task_success"] is False


# 功能：正确值和正确证据分别计分，旧值复活得到独立标记
# 设计：使用正确值加错误来源及旧值加旧来源，防止以引用存在代替支持关系
def test_score_distinguishes_values_and_evidence() -> None:
    gold = {"storage": {"value": "postgres", "evidence_id": "e2", "obsolete_values": ["sqlite"]}}
    wrong_source = score_answer(
        json.dumps(
            {
                "storage": {"value": "postgres", "evidence_id": "e1"},
            }
        ),
        gold,
        sample_task(),
    )
    assert wrong_source["task_success"] is True
    assert wrong_source["evidence_accuracy"] == 0
    stale = score_answer(
        json.dumps(
            {
                "storage": {"value": "sqlite", "evidence_id": "e1"},
            }
        ),
        gold,
        sample_task(),
    )
    assert stale["stale_revivals"] == 1
    assert stale["constraint_violations"] == 1


# 功能：模型调用与在途额度预留共同受总预算约束
# 设计：第二次调用在第一笔尚未结算时触及上限，防止并发超额和失败后免费重试
def test_budget_reserves_inflight_and_retains_unknown_usage() -> None:
    budget = Budget(max_calls=2, max_tokens=3000)
    reservation = budget.reserve(500, 200)
    with pytest.raises(BudgetExceeded):
        budget.reserve(500, 200)
    budget.settle(reservation, 100, 50)
    budget.reserve(500, 200)
    with pytest.raises(BudgetExceeded):
        budget.reserve(1, 1)


# 功能：拒绝输入公共任务中的隐藏标准答案
# 设计：向可发送模型的数据模型注入 expected，防止加载器宽松忽略后发生泄漏
def test_public_schema_rejects_gold() -> None:
    data = sample_task().model_dump()
    data["expected"] = {"storage": "postgres"}
    with pytest.raises(ValueError):
        MemoryTask.model_validate(data)


# 功能：改变 gold 不改变任何模型请求，且压缩看不到未来事件或最终任务
# 设计：记录两遍离线调用，逐字比较提示并将事件分轮，排除标准答案和未来泄漏
async def test_offline_pipeline_has_no_gold_or_future_leakage(tmp_path: Path) -> None:
    task = sample_task()
    task.episodes = [[event] for event in task.episodes[0]]
    prompts = []
    for index, value in enumerate(("postgres", "secret_gold_canary")):
        output = tmp_path / str(index)
        output.mkdir()
        model = Model("offline", Budget(30, 100_000), output, offline=True)
        rows = await run_case(
            task,
            {"storage": {"value": value, "evidence_id": "e2"}},
            model,
            ["summary", "retrieval", "versioned"],
            2048,
            512,
            3,
            0,
            1,
        )
        assert len(rows) == 3
        assert aggregate(rows)["mode"] == "offline"
        by_method = {row["method"]: row for row in rows}
        assert by_method["retrieval"]["summary"] == by_method["versioned"]["summary"]
        calls = [json.loads(line) for line in (output / "calls.jsonl").read_text().splitlines()]
        first = next(call for call in calls if call["label"].endswith("large/0"))
        assert "postgres" not in first["prompt"]
        assert task.query not in first["prompt"]
        prompts.append([(call["system"], call["prompt"]) for call in calls])
        await model.close()
    assert prompts[0] == prompts[1]


# 功能：截断但已计费的模型响应计入每个方法的实际成本
# 设计：模拟返回已知 usage 后因 max_tokens 失败，捕获异常路径把费用清零的问题
async def test_incomplete_response_usage_stays_in_arm_totals(tmp_path: Path) -> None:
    model = Model("test-model", Budget(10, 100_000), tmp_path, offline=True)
    model.offline = False
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="partial")],
        stop_reason="max_tokens",
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=512,
            cache_read_input_tokens=20,
            cache_creation_input_tokens=0,
        ),
        model="test-model",
    )
    client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=response)), close=AsyncMock()
    )
    model.client = client
    rows = await run_case(
        sample_task(),
        {"storage": {"value": "postgres", "evidence_id": "e2"}},
        model,
        ["summary"],
        2048,
        512,
        0,
        0,
        1,
    )
    assert rows[0]["status"] == "summary_error"
    assert rows[0]["input_tokens"] == 120
    assert rows[0]["output_tokens"] == 512
    assert rows[0]["summary_calls"] == 1
    assert model.budget.accounted_tokens == 632
    await model.close()
    client.close.assert_awaited_once()


# 功能：预算不足时保留所有待比较方法的失败行
# 设计：只允许一次压缩调用，检验方法分母不随执行失败缩水
async def test_budget_failure_keeps_every_arm(tmp_path: Path) -> None:
    model = Model("offline", Budget(1, 100_000), tmp_path, offline=True)
    rows = await run_case(
        sample_task(),
        {"storage": {"value": "postgres", "evidence_id": "e2"}},
        model,
        ["summary", "retrieval", "versioned"],
        2048,
        512,
        0,
        0,
        1,
    )
    assert len(rows) == 3
    assert all(row["status"] != "ok" for row in rows)
    assert model.budget.calls == 1
    await model.close()


@pytest.mark.parametrize("baseline", ["retrieval", "no_evidence"])
# 功能：配对汇总保留失败样本、消融组与双方互有胜负的信息
# 设计：构造两道题一胜一负，防止只报增强方案占优或显示未运行的对照
def test_aggregate_reports_paired_losses(baseline: str) -> None:
    rows = []
    for index in range(2):
        for method in ("summary", baseline, "versioned"):
            rows.append(
                {
                    "task_id": str(index),
                    "repeat": 0,
                    "method": method,
                    "category": "test",
                    "mode": "live",
                    "task_success": (index == 0) == (method == "versioned"),
                    "constraint_violations": 1,
                    "field_count": 3,
                    "evidence_accuracy": 0,
                    "stale_revivals": 0,
                    "valid": True,
                    "status": "ok",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "pipeline_latency_s": 1,
                }
            )
    result = aggregate(rows)
    assert set(result["paired"]) == {"summary", baseline}
    assert result["paired"][baseline] == {
        "versioned_wins": 1,
        "versioned_losses": 1,
        "ties": 0,
    }


# 功能：移除原始证据后不能从版本头直接复制答案值
# 设计：摘要置空，检查三个候选值都不可见，防止元数据成为隐藏答案通道
def test_no_evidence_ablation_cannot_copy_values() -> None:
    memory = build_memory(sample_task(), "", "no_evidence", 2048)
    assert all(value not in memory for value in ("sqlite", "postgres", "redis"))
    assert "e2" in memory


# 功能：完整离线 CLI 生成冻结配置、调用明细和明确无质量结论的报告
# 设计：使用正式数据端到端执行两题，并核对计划调用数与实际调用数
def test_offline_cli_writes_auditable_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "offline"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pilot",
            "--output",
            str(output),
            "--limit",
            "2",
            "--methods",
            "summary",
            "--noise-lines",
            "1",
        ],
    )
    main()
    manifest = json.loads((output / "manifest.json").read_text())
    report = json.loads((output / "summary.json").read_text())
    assert manifest["planned_calls"] == report["budget"]["calls"] == 8
    assert report["mode"] == "offline" and "methods" not in report
    assert "No model accuracy" in (output / "REPORT.md").read_text()
    assert len((output / "calls.jsonl").read_text().splitlines()) == 8


# 功能：标准答案损坏时在任何客户端创建或费用产生前拒绝执行
# 设计：保持字段存在但清空其结构，复现仅验证顶层字段的缺口
def test_malformed_gold_rejected_before_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = json.loads(Path("experiments/constraint_memory/answers.json").read_text())
    first = next(iter(answers))
    answers[first][next(iter(answers[first]))] = {}
    path = tmp_path / "bad-answers.json"
    path.write_text(json.dumps(answers))
    output = tmp_path / "should-not-exist"
    monkeypatch.setattr(sys, "argv", ["pilot", "--output", str(output), "--answers", str(path)])
    with pytest.raises(ValueError):
        main()
    assert not output.exists()
