from __future__ import annotations

import asyncio
import json
import subprocess


async def test_ping_returns_pong(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
    req = {
        "jsonrpc": "2.0",
        "id": "test-1",
        "method": "core.ping",
        "params": {"client": "test/0.0.1"},
    }
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()

    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    writer.close()
    await writer.wait_closed()

    resp = json.loads(line)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == "test-1"
    assert "result" in resp
    assert resp["result"]["server_version"] == "0.0.1"
    assert resp["result"]["uptime_ms"] >= 0
    assert "received_at" in resp["result"]


async def test_unknown_method_returns_error(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
    req = {
        "jsonrpc": "2.0",
        "id": "test-2",
        "method": "core.nonexistent",
        "params": {},
    }
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()

    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    writer.close()
    await writer.wait_closed()

    resp = json.loads(line)
    assert "error" in resp
    assert resp["error"]["code"] == -32601  # METHOD_NOT_FOUND


async def test_invalid_json_returns_error(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
    writer.write(b"not valid json\n")
    await writer.drain()

    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    writer.close()
    await writer.wait_closed()

    resp = json.loads(line)
    assert "error" in resp
    assert resp["error"]["code"] == -32700  # PARSE_ERROR
