from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


class McpServerUnavailableError(Exception):
    pass


@dataclass
class McpToolDef:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


# 通过 stdio 或 TCP 与 MCP server 通信的 JSON-RPC 2.0 客户端
class McpClient:
    def __init__(self) -> None:
        self._id = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._transport = ""
        self._lock = asyncio.Lock()

    # 启动 stdio 子进程并完成 MCP initialize 握手
    async def connect_stdio(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> None:
        import os
        merged_env = {**os.environ, **(env or {})}
        self._proc = await asyncio.create_subprocess_exec(
            command, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        self._reader = self._proc.stdout
        self._writer_proc = self._proc.stdin
        self._transport = "stdio"
        await self._initialize()

    # 通过 TCP 连接到 MCP server 并完成 initialize 握手
    async def connect_tcp(self, host: str, port: int) -> None:
        self._reader, tcp_writer = await asyncio.open_connection(host, port)
        self._tcp_writer = tcp_writer
        self._transport = "tcp"
        await self._initialize()

    # 发送 initialize 请求完成 MCP 握手
    async def _initialize(self) -> None:
        await self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mini-claude", "version": "0.1"},
        })
        await self._notify("notifications/initialized", {})

    # 列出 MCP server 提供的工具定义
    async def list_tools(self) -> list[McpToolDef]:
        response = await self._call("tools/list", {})
        tools = []
        for t in response.get("tools", []):
            tools.append(McpToolDef(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            ))
        return tools

    # 调用 MCP server 上的工具，返回第一个 text 类型内容
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            response = await self._call("tools/call", {"name": name, "arguments": arguments})
        except McpServerUnavailableError:
            raise
        except Exception as exc:
            raise McpServerUnavailableError(str(exc)) from exc
        for item in response.get("content", []):
            if item.get("type") == "text":
                return str(item["text"])
        return ""

    # 关闭连接并终止 stdio 子进程
    async def close(self) -> None:
        if self._transport == "stdio" and self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        elif self._transport == "tcp":
            writer = getattr(self, "_tcp_writer", None)
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    # 发送 JSON-RPC 请求并等待响应
    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        req_id = self._id
        request = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        async with self._lock:
            await self._write_line(json.dumps(request))
            while True:
                line = await self._read_line()
                if not line:
                    raise McpServerUnavailableError("MCP server closed connection")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(f"MCP error: {msg['error']}")
                    result: dict[str, Any] = msg.get("result", {})
                    return result

    # 发送 JSON-RPC 通知（无响应）
    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        notification = {"jsonrpc": "2.0", "method": method, "params": params}
        await self._write_line(json.dumps(notification))

    # 向 MCP server 写入一行 JSON
    async def _write_line(self, line: str) -> None:
        data = (line + "\n").encode()
        if self._transport == "stdio":
            w = self._proc.stdin if self._proc else None
            if w is None:
                raise McpServerUnavailableError("stdio writer unavailable")
            w.write(data)
            await w.drain()
        elif self._transport == "tcp":
            w = getattr(self, "_tcp_writer", None)
            if w is None:
                raise McpServerUnavailableError("tcp writer unavailable")
            w.write(data)
            await w.drain()

    # 从 MCP server 读取一行 JSON（跳过空行）
    async def _read_line(self) -> str:
        if self._reader is None:
            raise McpServerUnavailableError("reader unavailable")
        try:
            data = await asyncio.wait_for(self._reader.readline(), timeout=30.0)
        except TimeoutError:
            raise McpServerUnavailableError("MCP server read timeout")
        return data.decode().strip()
