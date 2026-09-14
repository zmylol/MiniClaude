from __future__ import annotations

import json

import pytest

from mini_claude.evaluation.constraint_memory import (
    Budget,
    BudgetExceeded,
    MemoryTask,
    build_memory,
    clip_utf8,
    score_answer,
)


# 创建包含旧决定、已确认更新和未批准建议的公开任务
def sample_task() -> MemoryTask:
    return MemoryTask.model_validate({
        "id": "sample", "category": "proposal_not_approval",
        "query": "Choose storage for the release.", "fields": ["storage"],
        "episodes": [[
            {"id": "e1", "role": "user", "text": "Use sqlite for storage.",
             "decisions": [{"key": "storage", "value": "sqlite",
                            "status": "confirmed", "supersedes": []}]},
            {"id": "e2", "role": "user", "text": "Replace sqlite with postgres.",
             "decisions": [{"key": "storage", "value": "postgres",
                            "status": "confirmed", "supersedes": ["e1"]}]},
            {"id": "e3", "role": "assistant", "text": "I suggest redis storage.",
             "decisions": [{"key": "storage", "value": "redis",
                            "status": "proposed", "supersedes": ["e2"]}]},
        ]],
    })


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


@pytest.mark.parametrize("method", ["summary", "retrieval", "versioned", "no_versions", "no_evidence"])
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
    gold = {"storage": {"value": "postgres", "evidence_id": "e2"}}
    wrong_source = score_answer(json.dumps({
        "storage": {"value": "postgres", "evidence_id": "e1"},
    }), gold, sample_task())
    assert wrong_source["task_success"] is True
    assert wrong_source["evidence_accuracy"] == 0
    stale = score_answer(json.dumps({
        "storage": {"value": "sqlite", "evidence_id": "e1"},
    }), gold, sample_task())
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
