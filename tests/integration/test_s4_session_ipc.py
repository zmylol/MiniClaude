from __future__ import annotations

import asyncio
import json
import subprocess


# 发送一条 JSON-RPC 请求并返回响应对象
async def _send_recv(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    params: dict,
    req_id: str = "1",
) -> dict:
    req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    return json.loads(line)


# 功能：验证 daemon 暴露 session 生命周期命令与 run.cancel 控制命令
# 设计：用不存在的 run_id 验证取消 handler 注册，避免触发真实 LLM，同时覆盖协议序列化与 session 持久化
async def test_session_create_history_close_over_ipc(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)

    created = await _send_recv(
        reader,
        writer,
        "session.create",
        {"mode": "chat", "title": "ipc test"},
        req_id="create",
    )
    assert "result" in created, created
    session_id = created["result"]["session_id"]
    assert created["result"]["status"] == "active"

    history = await _send_recv(
        reader,
        writer,
        "session.get_history",
        {"session_id": session_id},
        req_id="history",
    )
    assert history["result"]["messages"] == []

    cancelled = await _send_recv(
        reader,
        writer,
        "run.cancel",
        {"run_id": "run-not-running"},
        req_id="cancel",
    )
    assert cancelled["result"] == {"cancelled": False}

    closed = await _send_recv(
        reader,
        writer,
        "session.close",
        {"session_id": session_id},
        req_id="close",
    )
    assert closed["result"]["status"] == "closed"

    writer.close()
    await writer.wait_closed()
