# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install / sync dependencies
uv sync

# Lint
uv run ruff check src tests scripts
uv run mypy src

# Tests
uv run pytest tests/unit -v           # unit only (fast, no daemon)
uv run pytest tests/integration -v    # needs no running daemon; fixture spawns one
uv run pytest tests/ -v               # all

# Single test
uv run pytest tests/unit/test_envelope.py::test_request_roundtrip -v

# Regenerate WIRE_PROTOCOL.md after changing bus models
uv run python scripts/gen_protocol_doc.py

# Verify WIRE_PROTOCOL.md is in sync (used in CI equivalent)
uv run python scripts/gen_protocol_doc.py --check

# Run daemon manually
uv run mini-core                        # foreground; Ctrl+C to stop
MINI_PORT=8000 uv run mini-core        # override port

# Send a ping
uv run mini ping
uv run mini --version
```

## Architecture

This is a **dual-process** local AI agent system. `mini-core` is a persistent daemon; `mini` and `mini-tui` are clients that connect to it over a Unix domain socket.

```
mini-core (daemon)
  └─ listens on 127.0.0.1:7437 (TCP)
       ↑ JSON-RPC 2.0 NDJSON
mini (CLI)   mini-tui (TUI, S2+)
```

### Protocol layer (`src/mini_claude/core/bus/`)

All IPC messages are typed pydantic v2 models with a **discriminated union on the `type` field**. This is the contract boundary — adding a new command or event means adding a new model class to `commands.py` or `events.py` and extending the `Command`/`Event` union.

- `envelope.py` — `JsonRpcRequest`, `JsonRpcSuccess`, `JsonRpcError`, error code constants, `make_error()`
- `commands.py` — `Command` union; currently only `PingCommand` + `PongResult`
- `events.py` — `Event` union; currently only `CoreStartedEvent`

`WIRE_PROTOCOL.md` is **generated** from these models by `scripts/gen_protocol_doc.py`. Always regenerate and commit it after changing bus models.

### Transport layer (`src/mini_claude/core/transport/`)

- `socket_server.py` — TCP server (`asyncio.start_server`); reads NDJSON lines, dispatches to registered `CommandHandler`s, handles JSON-RPC error cases. On `start()`, probes `host:port` first — errors if another daemon is already listening. Handlers registered via `server.register("method.name", handler_fn)`.

### Config (`src/mini_claude/core/config.py`)

Four-tier priority: **built-in defaults → `~/.mini/config.toml` → `.env` → env vars**.

S0 keys: `host` (default `127.0.0.1`), `port` (default `7437`), `log_level`, `log_file`. Config file is silently skipped if absent; unknown keys cause a hard exit.

Relevant env vars: `MINI_CONFIG`, `MINI_HOST`, `MINI_PORT`, `MINI_LOG_LEVEL`, `MINI_LOG_FILE`, `MINI_LOG_FORMAT`.

### Daemon entry (`src/mini_claude/core/app.py`)

`CoreApp.run()` is the single async entry point: loads config → sets up logging → creates `SocketServer` → registers handlers → waits for `SIGINT`/`SIGTERM` → calls `server.stop()`. Adding new handlers: instantiate a handler method on `CoreApp` and call `server.register()`.

### Testing

Integration tests in `tests/conftest.py` spawn a real daemon subprocess using a random free port (via `free_port` fixture). The fixture finds a free port, releases it, passes it to the daemon via `MINI_PORT`, then polls `asyncio.open_connection` until the daemon is ready.

### Code style

All functions must have a **single-line Chinese comment** immediately above the `def` line explaining what the function does. Example:

```python
# 发送 JSON-RPC 响应并刷新写缓冲区
async def _send(self, writer: asyncio.StreamWriter, msg: BaseModel) -> None:
    ...
```

Do not write multi-line docstrings; one concise Chinese line is enough.

### Design docs (outside the repo)

The planning documents live in `../docs/` (sibling of this repo, not committed here):
- `agent_development_plan.md` — staged development roadmap S0–S8
- `s0_implementation_plan.md` — detailed S0 decisions and rationale
- `agent_functional_outline.md` — full feature catalogue
