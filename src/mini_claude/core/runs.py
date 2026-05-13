from __future__ import annotations

from pathlib import Path

RUNS_DIR = Path("runs")


# 返回指定 run_id 对应的目录路径
def run_dir(run_id: str) -> Path:
    return RUNS_DIR / run_id


# 返回指定 run_id 的事件日志文件路径
def events_file(run_id: str) -> Path:
    return run_dir(run_id) / "events.jsonl"
