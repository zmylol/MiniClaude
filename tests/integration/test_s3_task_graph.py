from __future__ import annotations

import asyncio
import json
import subprocess


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


# 功能：验证 task.create 返回 pending 状态的 task_id，task.list 能查到该任务
# 设计：通过 wire 协议直接与真实 daemon 交互，覆盖 CoreApp → TaskManager → 响应的完整链路
async def test_task_create_and_list(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)

    resp = await _send_recv(
        reader, writer,
        "task.create",
        {"type": "task.create", "goal": "test task"},
        req_id="c1",
    )
    assert "result" in resp, resp
    task_id = resp["result"]["task_id"]
    assert resp["result"]["state"] == "pending"

    resp2 = await _send_recv(reader, writer, "task.list", {}, req_id="l1")
    assert "result" in resp2
    tasks = resp2["result"]["tasks"]
    ids = [t["task_id"] for t in tasks]
    assert task_id in ids

    writer.close()
    await writer.wait_closed()


# 功能：验证 task.create 时传入不存在的 parent_id 返回 TASK_NOT_FOUND 错误码（-32003）
# 设计：校验错误码精确值，确认 HandlerError → SocketServer → JSON-RPC error 的转换链路完整
async def test_task_create_invalid_parent_returns_error(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)

    resp = await _send_recv(
        reader, writer,
        "task.create",
        {"type": "task.create", "goal": "child", "parent_id": "nonexistent"},
        req_id="e1",
    )
    assert "error" in resp, resp
    assert resp["error"]["code"] == -32003  # TASK_NOT_FOUND

    writer.close()
    await writer.wait_closed()


# 功能：验证 task.start 对不存在 task_id 返回 TASK_NOT_FOUND 错误码
# 设计：start 路径与 create 路径使用相同的错误转换机制，覆盖 start handler 的错误分支
async def test_task_start_not_found(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)

    resp = await _send_recv(
        reader, writer,
        "task.start",
        {"task_id": "task-does-not-exist"},
        req_id="s1",
    )
    assert "error" in resp, resp
    assert resp["error"]["code"] == -32003  # TASK_NOT_FOUND

    writer.close()
    await writer.wait_closed()


# 功能：验证创建带 depends_on 的任务，start 时依赖未满足返回 TASK_DEPENDENCY_ERROR（-32002）
# 设计：先创建 dep（不启动），再创建 child depends_on=[dep]，对 child 调用 start 应被阻止
async def test_task_start_blocked_by_dependency(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)

    r1 = await _send_recv(
        reader, writer, "task.create",
        {"type": "task.create", "goal": "dep task"},
        req_id="dep",
    )
    dep_id = r1["result"]["task_id"]

    r2 = await _send_recv(
        reader, writer, "task.create",
        {"type": "task.create", "goal": "child task", "depends_on": [dep_id]},
        req_id="child",
    )
    child_id = r2["result"]["task_id"]

    resp = await _send_recv(
        reader, writer,
        "task.start",
        {"task_id": child_id},
        req_id="start",
    )
    assert "error" in resp, resp
    assert resp["error"]["code"] == -32002  # TASK_DEPENDENCY_ERROR

    writer.close()
    await writer.wait_closed()


# 功能：验证 task.cancel 对 pending 任务有效，状态变为 cancelled
# 设计：pending→cancelled 是直接迁移（无活跃 asyncio.Task），确认非 running 状态的取消路径
async def test_task_cancel_pending(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)

    r1 = await _send_recv(
        reader, writer, "task.create",
        {"type": "task.create", "goal": "cancel me"},
        req_id="c1",
    )
    task_id = r1["result"]["task_id"]

    resp = await _send_recv(
        reader, writer, "task.cancel",
        {"task_id": task_id},
        req_id="cancel",
    )
    assert "result" in resp, resp
    assert resp["result"]["state"] == "cancelled"

    writer.close()
    await writer.wait_closed()
