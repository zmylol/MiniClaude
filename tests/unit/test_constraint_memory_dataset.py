import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

DATA_DIR = Path(__file__).resolve().parents[2] / "experiments" / "constraint_memory"
CATEGORIES = {
    "latest_update", "reverted_decision", "proposal_not_approval",
    "tool_output_not_authority", "scoped_update", "unchanged_control",
}


@pytest.fixture
# 从公开任务与独立答案文件加载手工构造的数据，不经过被测运行器
def dataset() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tasks = [json.loads(line) for line in (DATA_DIR / "tasks.jsonl").read_text().splitlines()]
    answers = json.loads((DATA_DIR / "answers.json").read_text())
    return tasks, answers


# 功能：数据覆盖六类各六个独立任务，公开输入不携带评测答案
# 设计：直接检查磁盘协议，防止模型输入意外混入 gold 或遗漏实验类别
def test_dataset_has_balanced_categories_and_separate_answers(dataset: Any) -> None:
    tasks, answers = dataset
    assert len(tasks) == 36
    assert Counter(task["category"] for task in tasks) == dict.fromkeys(CATEGORIES, 6)
    assert len({task["id"] for task in tasks}) == 36
    assert set(answers) == {task["id"] for task in tasks}
    assert len({task["query"] for task in tasks}) == 36
    for task in tasks:
        assert set(task) == {"id", "category", "query", "fields", "episodes"}
        assert len(task["fields"]) == len(set(task["fields"])) == 3
        assert all(isinstance(field, str) and field for field in task["fields"])
        assert len(task["episodes"]) == 3
        assert set(answers[task["id"]]) == set(task["fields"])


# 功能：公开事件遵循来源权限与因果顺序，证据不能引用未来或无关字段
# 设计：逐条核验原始数据的完整性，使错误标注无法被运行器的容错隐藏
def test_event_protocol_has_valid_authority_and_causal_references(dataset: Any) -> None:
    tasks, _ = dataset
    for task in tasks:
        seen: dict[str, dict[str, Any]] = {}
        roles = set()
        for episode in task["episodes"]:
            assert 3 <= len(episode) <= 5
            for event in episode:
                assert set(event) == {"id", "role", "text", "decisions"}
                assert event["id"] not in seen
                assert event["role"] in {"user", "assistant", "tool"}
                roles.add(event["role"])
                assert isinstance(event["text"], str) and event["text"].strip()
                assert isinstance(event["decisions"], list)
                for decision in event["decisions"]:
                    assert set(decision) == {"key", "value", "status", "supersedes"}
                    assert decision["key"] in task["fields"]
                    assert isinstance(decision["value"], str) and decision["value"]
                    assert decision["value"] in event["text"]
                    assert decision["status"] in {"confirmed", "proposed"}
                    if decision["status"] == "confirmed":
                        assert event["role"] == "user"
                    assert isinstance(decision["supersedes"], list)
                    for old_id in decision["supersedes"]:
                        assert old_id in seen
                        assert any(
                            old["key"] == decision["key"] and old["status"] == "confirmed"
                            for old in seen[old_id]["decisions"]
                        )
                seen[event["id"]] = event
        assert roles == {"user", "assistant", "tool"}


# 功能：每项 gold 都有正文可见的最新用户证据，控制任务不存在约束更新
# 设计：以文件中的来源和时间为独立检查依据，同时识别错误答案与伪控制组
def test_gold_is_grounded_in_latest_user_confirmation(dataset: Any) -> None:
    tasks, answers = dataset
    for task in tasks:
        events = [event for episode in task["episodes"] for event in episode]
        for field, answer in answers[task["id"]].items():
            assert set(answer) == {"value", "evidence_id"}
            confirmations = [
                (event, decision)
                for event in events
                for decision in event["decisions"]
                if decision["key"] == field and decision["status"] == "confirmed"
            ]
            assert confirmations
            event, decision = confirmations[-1]
            assert answer == {"value": decision["value"], "evidence_id": event["id"]}
            assert event["role"] == "user"
            assert answer["value"] in event["text"]
            if task["category"] == "unchanged_control":
                assert len(confirmations) == 1
        updates = [
            decision for event in events for decision in event["decisions"]
            if decision["status"] == "confirmed" and decision["supersedes"]
        ]
        assert bool(updates) == (task["category"] != "unchanged_control")


# 功能：撤回与局部更新任务真实具备类别定义中的历史结构
# 设计：检查更新序列和字段数量，避免仅凭类别标签将普通改值任务冒充特殊场景
def test_reversions_and_scoped_changes_are_present(dataset: Any) -> None:
    tasks, _ = dataset
    for task in tasks:
        confirmations = [
            decision for episode in task["episodes"] for event in episode
            for decision in event["decisions"] if decision["status"] == "confirmed"
        ]
        sequences = {
            field: [item["value"] for item in confirmations if item["key"] == field]
            for field in task["fields"]
        }
        if task["category"] == "reverted_decision":
            assert any(len(values) == 3 and values[0] == values[2] != values[1]
                       for values in sequences.values())
        if task["category"] == "scoped_update":
            assert sorted(len(values) for values in sequences.values()) == [1, 1, 2]
