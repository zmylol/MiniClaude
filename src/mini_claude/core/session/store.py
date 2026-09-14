from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mini_claude.core.session.model import Session

logger = logging.getLogger(__name__)

MessageContent = str | list[dict[str, Any]]


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionStore:
    # 初始化 session 文件存储根目录
    def __init__(self, root: Path) -> None:
        self._root = root.expanduser()
        self._root.mkdir(parents=True, exist_ok=True)

    # 返回指定 session 的目录路径
    def session_dir(self, sid: str) -> Path:
        if not re.fullmatch(r"sess-[A-Za-z0-9_-]{1,64}", sid):
            raise ValueError("invalid session id")
        path = self._root / sid
        if path.is_symlink() or path.resolve().parent != self._root.resolve():
            raise ValueError("invalid session directory")
        return path

    # 扫描会话元数据，跳过损坏记录和符号链接而不访问根目录之外的数据
    def list_sessions(self) -> list[Session]:
        sessions = []
        for path in self._root.iterdir():
            if not path.is_dir() or path.is_symlink() or not path.name.startswith("sess-"):
                continue
            try:
                session = self.read_meta(path.name)
                if session.id == path.name:
                    sessions.append(session)
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning("skip invalid session metadata: %s", path.name)
        return sessions

    # 删除已校验的会话目录及其历史和运行记录
    def delete(self, sid: str) -> None:
        shutil.rmtree(self.session_dir(sid))

    # 返回指定 session 下的 runs 目录路径
    def runs_dir(self, sid: str) -> Path:
        return self.session_dir(sid) / "runs"

    # 将 session meta 写入 meta.json
    def write_meta(self, session: Session) -> None:
        path = self.session_dir(session.id)
        path.mkdir(parents=True, exist_ok=True)
        (path / "meta.json").write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # 从 meta.json 读取 session meta
    def read_meta(self, sid: str) -> Session:
        path = self.session_dir(sid) / "meta.json"
        if path.is_symlink():
            raise ValueError("invalid session metadata")
        data = json.loads(path.read_text(encoding="utf-8"))
        return Session.from_dict(data)

    # 追加一条 Anthropic API 消息到 thread.jsonl
    def append_message(
        self,
        sid: str,
        role: str,
        content: MessageContent,
        run_id: str | None = None,
    ) -> None:
        row: dict[str, Any] = {"ts": _now(), "role": role, "content": content}
        if run_id is not None:
            row["run_id"] = run_id
        path = self.session_dir(sid)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "thread.jsonl").open("ab+") as f:
            # 中断可能留下未终止的记录，先分隔旧尾部以免吞掉本次新消息
            if f.seek(0, os.SEEK_END):
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    f.write(b"\n")
            f.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))

    # 批量追加一次 run 新产生的消息到 thread.jsonl
    def append_messages(
        self,
        sid: str,
        messages: list[dict[str, Any]],
        run_id: str,
    ) -> None:
        for msg in messages:
            self.append_message(
                sid,
                role=str(msg["role"]),
                content=msg["content"],
                run_id=run_id,
            )

    # 读取原始行号与消息，损坏行不改变后续记录的稳定位置
    def _read_records(self, sid: str) -> list[tuple[int, dict[str, Any]]]:
        path = self.session_dir(sid) / "thread.jsonl"
        if not path.exists():
            return []

        records: list[tuple[int, dict[str, Any]]] = []
        for line_no, line in enumerate(path.read_bytes().split(b"\n"), start=1):
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("skip broken thread row sid=%s line=%s", sid, line_no)
                continue
            if not isinstance(row, dict):
                logger.warning("skip non-object thread row sid=%s line=%s", sid, line_no)
                continue
            role = row.get("role")
            if role not in ("user", "assistant"):
                logger.warning(
                    "skip unknown thread role sid=%s line=%s role=%s",
                    sid,
                    line_no,
                    role,
                )
                continue
            records.append((line_no, {"role": role, "content": row.get("content", "")}))
        return records

    # 返回原始日志末尾位置，包括被读取器跳过的损坏记录
    def history_position(self, sid: str) -> int:
        path = self.session_dir(sid) / "thread.jsonl"
        data = path.read_bytes() if path.exists() else b""
        return data.count(b"\n") + int(bool(data) and not data.endswith(b"\n"))

    # 返回供界面展示的原始历史，不截短工具结果或删除未完成调用
    def read_history(self, sid: str) -> list[dict[str, Any]]:
        return [message for _, message in self._read_records(sid)]

    # 从已提交摘要及后续原始记录构造模型输入，兼容没有摘要的旧会话
    def read_messages(self, sid: str) -> list[dict[str, Any]]:
        records = self._read_records(sid)
        messages: list[dict[str, Any]] = []
        covered = 0
        checkpoint = self.session_dir(sid) / "context.json"
        if checkpoint.exists():
            try:
                data = json.loads(checkpoint.read_text(encoding="utf-8"))
                position = data["covered_through"]
                summary = data["messages"]
                if (
                    not isinstance(position, int) or position < 0
                    or position > self.history_position(sid)
                    or not isinstance(summary, list)
                    or not summary
                    or any(not isinstance(m, dict) or m.get("role") not in
                           ("user", "assistant") or "content" not in m for m in summary)
                ):
                    raise ValueError("invalid context checkpoint")
                covered = position
                messages = summary
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning("ignore invalid context checkpoint sid=%s", sid)
        messages.extend(message for position, message in records if position > covered)

        messages = self._repair_interrupted_tool_use(messages)
        from mini_claude.core.compact.budget import truncate_tool_results
        return truncate_tool_results(messages)

    # 仅在模型视图补齐中断工具的未知结果，保留原始记录与重启后的新请求
    def _repair_interrupted_tool_use(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        messages = list(messages)
        repaired: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            repaired.append(message)
            content = message.get("content")
            if message.get("role") != "assistant" or not isinstance(content, list):
                continue
            calls = [block["id"] for block in content if isinstance(block, dict)
                     and block.get("type") == "tool_use" and block.get("id")]
            following = messages[index + 1] if index + 1 < len(messages) else {}
            results = following.get("content") if following.get("role") == "user" else None
            completed = (
                {block.get("tool_use_id") for block in results
                 if isinstance(block, dict) and block.get("type") == "tool_result"}
                if isinstance(results, list) else set()
            )
            missing = [{
                "type": "tool_result", "tool_use_id": call, "is_error": True,
                "content": "Tool execution was interrupted before its result was saved. "
                           "Its effects are unknown; check current state before retrying.",
            } for call in calls if call not in completed]
            if not missing:
                continue
            logger.warning("recover interrupted tool results for model context: %s", len(missing))
            if isinstance(results, list):
                messages[index + 1] = {**following, "content": missing + results}
            else:
                repaired.append({"role": "user", "content": missing})
        return repaired

    # 原子替换模型摘要检查点，覆盖位置只能引用已经写入的原始历史
    def write_compacted(
        self, sid: str, messages: list[dict[str, Any]], covered_through: int | None = None,
    ) -> None:
        end = self.history_position(sid)
        covered = end if covered_through is None else covered_through
        if covered < 0 or covered > end:
            raise ValueError("summary cannot cover uncommitted history")
        directory = self.session_dir(sid)
        directory.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix="context-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"covered_through": covered, "messages": messages}, stream,
                          ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(directory / "context.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    # 读取 notes.md 全文，文件不存在时返回空字符串
    def read_notes(self, sid: str) -> str:
        path = self.session_dir(sid) / "notes.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    # 将一条主动笔记追加到 notes.md
    def append_note(self, sid: str, content: str, run_id: str) -> None:
        path = self.session_dir(sid)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "notes.md").open("a", encoding="utf-8") as f:
            f.write(f"## Note ({_now()}, {run_id})\n{content}\n\n")
