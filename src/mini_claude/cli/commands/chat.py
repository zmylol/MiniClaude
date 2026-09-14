from __future__ import annotations

import asyncio
import sys
from typing import Any

from mini_claude.cli.commands.run import ResponseBuffer
from mini_claude.core.config import MiniConfig
from mini_claude.core.transport.socket_client import IpcError, SocketClient

_DECISION_MAP: dict[str, str] = {
    "y": "allow_once",
    "a": "always_allow",
    "n": "deny_once",
    "d": "always_deny",
}


class ChatPrinter:
    # 初始化 chat 模式的流式输出状态和待审批权限请求
    def __init__(self) -> None:
        self._inline = False
        self.pending_permission_id: str | None = None
        self.pending_permission_run: str | None = None
        self._permissions: dict[tuple[str, str], None] = {}
        self._resolved_permissions: set[tuple[str, str]] = set()
        self.submitting_permissions: set[tuple[str, str]] = set()
        self.session_id: str | None = None
        self._runs: set[str] = set()
        self._children: set[str] = set()
        self._responses = ResponseBuffer()

    # 移除一个运行的审批并继续展示其他待审批，避免相同工具 ID 相互覆盖
    def settle_permission(self, run_id: str, tool_id: str) -> None:
        key = (run_id, tool_id)
        self._permissions.pop(key, None)
        self._resolved_permissions.add(key)
        self.submitting_permissions.discard(key)
        pending = next(iter(self._permissions), None)
        self.pending_permission_run = pending[0] if pending else None
        self.pending_permission_id = pending[1] if pending else None

    # 若当前 LLM token 尚未换行，则补一个换行
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    # 按事件类型打印 chat 输出、等待提示和权限审批请求
    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        run_id = str(event.get("run_id") or "")
        if self.session_id is not None:
            owner = event.get("session_id")
            if owner and owner != self.session_id:
                return
            if not owner and run_id not in self._runs:
                if event.get("parent_run_id") not in self._runs:
                    return
            if run_id:
                self._runs.add(run_id)
        if event.get("parent_run_id") or (
            event.get("root_run_id") and event["root_run_id"] != run_id
        ):
            self._children.add(run_id)
        if run_id in self._children and not t.startswith("permission."):
            return
        if self._responses.handle(event):
            return
        if t == "tool.call_started":
            self._ensure_newline()
            print(f"[tool] {event.get('tool_name', '')}")
        elif t == "permission.requested":
            key = (run_id, str(event.get("tool_use_id", "")))
            if key in self._permissions or key in self._resolved_permissions:
                return
            self._permissions[key] = None
            self._ensure_newline()
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            tool_use_id = str(event.get("tool_use_id", ""))
            print(f"[permission] {tool_name}  {param_preview}")
            print("  y=allow once  a=always allow  n=deny once  d=always deny")
            self.pending_permission_id = tool_use_id
            self.pending_permission_run = run_id
        elif t in {"permission.granted", "permission.denied"}:
            key = (run_id, str(event.get("tool_use_id", "")))
            if key not in self._resolved_permissions:
                print(f"[permission] {key[1]} {event.get('decision', '')}")
            self.settle_permission(*key)
        elif t == "session.waiting_for_input":
            self._ensure_newline()
            print("[waiting for input]")
        elif t == "session.closed":
            self._ensure_newline()
            self._permissions.clear()
            self.pending_permission_id = None
            self.pending_permission_run = None
            print("session closed.")


# 在线程池中读取 stdin，避免阻塞 socket event loop
async def _readline(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


# 异步核心：创建 chat session，循环读取用户输入并发送到 daemon；权限请求时优先处理审批
async def _chat_async(config: MiniConfig) -> int:
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    printer = ChatPrinter()
    client.on_event(printer.handle)
    loop_task = asyncio.create_task(client.run_event_loop())

    try:
        await client.send_command(
            "event.subscribe",
            {
                "topics": ["session.*", "run.*", "step.*", "tool.*", "llm.*", "permission.*"],
                "scope": "global",
            },
        )
        created = await client.send_command("session.create", {"mode": "chat"})
        session_id = str(created["session_id"])
        printer.session_id = session_id
        print(f"[session: {session_id}]")

        while True:
            try:
                line = await _readline("> ")
            except (EOFError, KeyboardInterrupt):
                break
            content = line.strip()
            if not content:
                continue

            # 有待审批的权限请求时，将用户输入解释为决策而非聊天消息
            if printer.pending_permission_id:
                decision = _DECISION_MAP.get(content.lower())
                if decision is None:
                    print("  enter y (allow once), a (always allow), "
                          "n (deny once), d (always deny)")
                    continue
                tool_use_id = printer.pending_permission_id
                permission_run = printer.pending_permission_run or ""
                key = (permission_run, tool_use_id)
                if key in printer.submitting_permissions:
                    print("[permission] waiting for server decision")
                    continue
                printer.submitting_permissions.add(key)
                try:
                    await client.send_command(
                        "permission.respond",
                        {"tool_use_id": tool_use_id, "decision": decision,
                         "run_id": permission_run or None},
                    )
                except (IpcError, RuntimeError, OSError) as exc:
                    printer.submitting_permissions.discard(key)
                    print(f"approval failed: {exc}", file=sys.stderr)
                continue

            await client.send_command(
                "session.send_message",
                {"session_id": session_id, "content": content},
            )

        await client.send_command("session.close", {"session_id": session_id})
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await client.close()
    return 0


# 执行 mini chat 命令
def cmd_chat(config: MiniConfig) -> None:
    try:
        exit_code = asyncio.run(_chat_async(config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
