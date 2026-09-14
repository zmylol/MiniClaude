# MiniClaude

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![uv](https://img.shields.io/badge/package%20manager-uv-654FF0)](https://docs.astral.sh/uv/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Local daemon](https://img.shields.io/badge/runtime-local%20daemon-blue)](#architecture)
[![MCP ready](https://img.shields.io/badge/tools-MCP%20ready-orange)](#核心能力)

MiniClaude 是一个本地优先的 AI Agent 系统：`mini-core` 作为常驻守护进程负责
session、工具、事件流、权限审批和 LLM 调用，`mini` 与 `mini-tui` 作为客户端通过
TCP loopback 与它通信。

它不是一个只包了一层 API 的命令行脚本，而是一套可观察、可扩展、可持续迭代的
agent runtime。你可以用 CLI 做脚本化验证，用 TUI 进行多轮交互，也可以通过
JSON-RPC/NDJSON 协议接入新的前端或自动化工具。

> 当前版本：`0.0.1`。项目仍处于快速迭代阶段，README 以当前仓库中的实现为准。

## 目录

- [为什么是 MiniClaude](#为什么是-miniclaude)
- [核心能力](#核心能力)
- [Roadmap Preview](#roadmap-preview)
- [快速开始](#快速开始)
- [常用命令](#常用命令)
- [Architecture](#architecture)
- [配置与本地数据](#配置与本地数据)
- [开发与验证](#开发与验证)
- [文档地图](#文档地图)

## 为什么是 MiniClaude

MiniClaude 的核心设计目标是把一个 agent 拆成清晰的本地系统，而不是把所有状态塞进
一次性的 CLI 进程里。

- **长驻 core**：`mini-core` 统一管理 session、run、trace、权限策略和 MCP server
  生命周期。
- **双入口交互**：`mini-tui` 是主要交互界面，`mini` CLI 适合快速测试、脚本调用和调试。
- **事件驱动**：客户端订阅 `run.*`、`tool.*`、`llm.token`、`permission.*` 等事件，
  可以实时渲染 agent 的执行过程。
- **可审计运行轨迹**：trace 记录 IPC、事件和 LLM 层面的关键数据，方便回放、排错和性能分析。
- **权限优先**：工具调用经过权限审批，可选择一次允许、始终允许、一次拒绝或始终拒绝。
- **可扩展工具面**：内置文件、shell、任务、笔记和 subagent 工具，同时支持通过 MCP 接入外部工具。

## 核心能力

| 能力 | 当前实现 |
|------|----------|
| Core daemon | `mini-core` 监听 `127.0.0.1:7437`，处理 JSON-RPC 命令和事件广播 |
| CLI | `mini ping`、`mini chat`、`mini run`、`mini core`、`mini trace` |
| TUI | `mini-tui` 提供 Textual 终端界面，支持聊天、流式输出、工具块、权限审批和 replay |
| One-shot run | `mini run --goal "..."` 创建一次性 session 并执行 agent 任务 |
| Multi-turn chat | `mini chat` / `mini-tui` 创建可持续对话的 chat session |
| Event stream | run、step、tool、LLM token、permission、context compaction 等事件实时推送 |
| Trace | 默认写入 `~/.mini/traces/daemon.jsonl`，可用 `mini trace` 查看和过滤 |
| Built-in tools | `read_file`、`write_file`、`list_dir`、`bash`、task 系列、`note_save`、subagent 工具 |
| 联网工具 | DeepSeek 官方端点使用原生搜索，其他后端使用 DDGS；另有 Trafilatura 网页正文提取、按任务隔离的 Playwright MCP 浏览器；详见 [联网能力](docs/NETWORKING.md) |
| Parallel tools | 同一轮的多个工具调用默认并发调度；结果按调用顺序回传，依赖操作需分轮发起 |
| Permissions | 工具调用支持一次性/持久化审批，策略存储在 `~/.mini/policy.toml` |
| Memory/context | 读取 `~/.mini/context.md` 与 `.mini/context.md`，session notes 可跨轮注入上下文 |
| Compaction | 支持 session 上下文压缩，TUI 中可使用 `/compact` |
| MCP | 支持 stdio / TCP MCP server，发现的 MCP 工具会注入 agent 工具注册表 |

## Roadmap Preview

MiniClaude 的 S8 桌面工作台与工具并行执行已完成，后续路线围绕可靠任务执行逐步推进：

- **S8 — Desktop & Parallel Tools（已完成）**：桌面项目与会话管理、审批和停止、同轮工具并行。
- **S9 — Graph Runtime（下一主线）**：先交付静态串行任务图与桌面执行闭环，再开放受控并发。
- **证据记忆支撑线 — Memory v1**：显式保存决策、原始证据和预算化检索，支持后续历史恢复。
- **S10 — Critical Decisions**：先做单候选验证；双候选、Critic 和 Recovery 通过成本/质量评测后再启用。
- **S11 — Bounded Exploration**：先验证固定预算下的只读候选探索，动态 Local GraphLoop 后置。

> S8–S11 是内部能力里程碑，不对应包版本号、固定 GitHub Release 或承诺发布日期。
> 除明确标记已完成的 S8 外，其余为计划或实验能力；旧稿的 S8 Memory 已归入独立支撑线。

完整范围、非目标、公开演示和评测门槛见 [ROADMAP.md](./ROADMAP.md)。

## 快速开始

### 环境要求

| 依赖 | 版本 |
|------|------|
| 操作系统 | macOS / Linux |
| Python | 3.12.x |
| [uv](https://docs.astral.sh/uv/) | 0.4 或更高 |

如果还没有安装 `uv`：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Python 3.12 由 `uv` 自动管理，通常不需要手动安装。

### 安装依赖

```bash
git clone <repo-url> miniclaude
cd miniclaude
uv sync
```

### 配置本机环境

```bash
cp .env.example .env
```

如果只验证 daemon 连通性，默认配置即可。若要运行真正的 agent/chat，需要在 `.env`
中配置 Anthropic API key：

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

可选项：

```bash
MINI_LLM_DEFAULT_MODEL=claude-sonnet-4-6
MINI_MAX_STEPS=20
```

### 启动 core

推荐用 CLI 管理后台 daemon：

```bash
uv run mini core start
uv run mini core status
uv run mini ping
```

成功时 `mini ping` 会输出类似：

```text
pong server=0.0.1 uptime=12ms latency=2ms
```

也可以前台启动，适合开发时观察日志：

```bash
uv run mini-core
```

停止后台 daemon：

```bash
uv run mini core stop
```

### 打开 TUI

```bash
uv run mini-tui
```

TUI 会连接正在运行的 `mini-core`，创建 chat session，并实时显示 LLM token、工具调用、
权限审批和 session 状态。

回放历史 run：

```bash
uv run mini-tui --replay <run-id>
```

### 打开桌面应用

macOS 上可以使用独立的 MiniClaude 应用窗口，界面参考 Codex 桌面版，通过事件流
实时显示对话、工具执行和权限审批。应用自动连接或启动当前项目的 core。
支持系统文件夹选择、项目切换与从列表移除、文件和图片附件、重启续聊、停止任务、模型和权限设置，
以及 Git 改动、PR 列表、定时任务和 MCP 服务管理。

```bash
uv sync --extra desktop
uv run --extra desktop mini-desktop
```

生成可双击的本机应用入口：

```bash
.venv/bin/python scripts/build_macos_app.py
```

随后打开 `dist/MiniClaude.app`。此启动器依赖当前项目及 `.venv`。
界面能力、运行方式与架构见 [桌面应用说明](docs/DESKTOP.md)。

### 运行一次性任务

```bash
uv run mini run --goal "总结 README.md 的主要章节"
```

`mini run` 会订阅事件流、触发 agent run，并在终端中打印 step、tool、LLM token 和
最终状态。

### 测试工具并行执行

更新代码后重启 `mini-core`，在聊天界面发送以下提示词：

```text
请测试工具并行执行。下一条回复在同一轮中发出 3 个独立的 bash 工具调用，
每个调用分别执行下面的一条命令。这三项互不依赖，请同时发起。
不要合并命令，不要使用 &、后台子代理，也不要先运行探测命令。

python3 -c 'import time; s=time.time(); time.sleep(3); print("A", s, time.time())'
python3 -c 'import time; s=time.time(); time.sleep(3); print("B", s, time.time())'
python3 -c 'import time; s=time.time(); time.sleep(3); print("C", s, time.time())'

收到全部结果后，列出 A/B/C 的开始与结束时间，计算最晚开始时间是否早于
最早结束时间，并计算从最早开始到最晚结束的总耗时。根据实际输出判断是否并行。
```

如果出现审批，请及时批准三个调用。并行时三个执行区间应重叠，命令总耗时约 3 秒，
串行则约 9 秒；模型生成回复和人工审批的等待时间会影响聊天总耗时。
每个调用仍独立进行权限检查、超时处理和重试；停止任务会取消尚未完成的调用，
并保留已经完成的结果。

并发调度能让 Bash 子进程和不同 MCP 连接的等待重叠。同步文件工具的 I/O 仍在事件循环
中执行；同一 MCP 连接保留请求锁，调用按顺序执行以确保响应正确配对。

## 常用命令

### 用户命令

| 命令 | 用途 |
|------|------|
| `uv run mini --version` | 输出当前包版本 |
| `uv run mini ping` | 检查 CLI 到 core daemon 的连通性 |
| `uv run mini chat` | 启动 CLI 多轮聊天 session |
| `uv run mini run --goal "..."` | 执行一次 one-shot agent 任务 |
| `uv run mini core start` | 后台启动 `mini-core` |
| `uv run mini core status` | 查看 daemon 是否正在运行 |
| `uv run mini core stop` | 停止后台 daemon |
| `uv run mini trace` | 查看 trace 日志 |
| `uv run mini trace <run-id>` | 按 run ID 过滤 trace |
| `uv run mini trace --layer llm` | 只看 LLM 层 trace |
| `uv run mini trace --raw --follow` | 以 NDJSON 形式持续跟踪 trace |
| `uv run mini-tui` | 打开 Textual TUI |
| `uv run mini-tui --replay <run-id>` | 连接后回放历史 run 事件 |

### 开发命令

| 命令 | 用途 |
|------|------|
| `uv sync` | 安装/同步依赖 |
| `uv run ruff check src tests scripts` | 运行 lint |
| `uv run mypy src` | 运行严格类型检查 |
| `uv run pytest tests/unit -v` | 运行单元测试 |
| `uv run pytest tests/integration -v` | 运行集成测试 |
| `make docs` | 重新生成 `WIRE_PROTOCOL.md` |
| `make verify-s0` | 执行完整 S0 验证链路 |

## Architecture

```mermaid
flowchart LR
    CLI["mini CLI"]
    TUI["mini-tui"]
    Core["mini-core daemon"]
    Bus["Event bus"]
    Sessions["Session manager"]
    Runner["Agent loop"]
    Tools["Built-in tools"]
    MCP["MCP servers"]
    Anthropic["Anthropic API"]
    Trace["Trace/Event files"]

    CLI <-->|JSON-RPC 2.0 over TCP NDJSON| Core
    TUI <-->|JSON-RPC 2.0 over TCP NDJSON| Core

    Core --> Bus
    Core --> Sessions
    Core --> Trace
    Sessions --> Runner
    Runner --> Tools
    Runner --> MCP
    Runner --> Anthropic
    Runner --> Bus
    Bus --> CLI
    Bus --> TUI
```

### 运行模型

1. `mini-core` 启动后加载配置、初始化日志、trace、权限管理器、MCP server 和 session store。
2. 客户端通过 TCP loopback 连接 core，发送 JSON-RPC 2.0 命令，每条消息都是一行 NDJSON。
3. `mini run` 创建 one-shot session；`mini chat` 和 `mini-tui` 创建 chat session。
4. `SessionManager` 把用户消息交给 `AgentRunner`，runner 组装 LLM provider、工具注册表和执行上下文。
5. agent 执行过程中产生事件，core 通过 event bus 广播给已订阅的客户端，同时写入 run events 和 trace。
6. 需要敏感工具调用时，permission manager 会挂起调用并等待客户端审批。

### 协议边界

IPC 命令和事件模型定义在 `src/mini_claude/core/bus/`。`WIRE_PROTOCOL.md` 由
`scripts/gen_protocol_doc.py` 从这些模型生成，不应手动编辑。

## 配置与本地数据

配置优先级从低到高：

```text
内建默认值 -> ~/.mini/config.toml -> .mini/config.toml -> .env -> 系统环境变量
```

常用环境变量：

| 变量 | 默认值 | 用途 |
|------|--------|------|
| `MINI_CONFIG` | `~/.mini/config.toml` | 指定配置文件路径 |
| `MINI_HOST` | `127.0.0.1` | core daemon 监听地址 |
| `MINI_PORT` | `7437` | core daemon 监听端口 |
| `MINI_LOG_LEVEL` | `INFO` | 日志级别 |
| `MINI_LOG_FILE` | `~/.mini/logs/core.log` | core 日志文件 |
| `MINI_LOG_FORMAT` | `text` | 日志格式，支持 `text` / `json` |
| `MINI_LLM_DEFAULT_MODEL` | `claude-sonnet-4-6` | 默认 Anthropic 模型 |
| `MINI_MAX_STEPS` | `20` | 单次 agent run 最大步数 |
| `MINI_TRACE_ENABLED` | `true` | 是否启用 trace |
| `MINI_TRACE_FILE` | `~/.mini/traces/daemon.jsonl` | trace 文件位置 |
| `MINI_PERMISSION_TIMEOUT_S` | `60` | 权限审批超时时间，`0` 表示不超时 |
| `MINI_COMPACT_THRESHOLD` | `0` | 自动压缩触发阈值，`0` 表示禁用 |

本地数据位置：

| 路径 | 内容 |
|------|------|
| `~/.mini/logs/core.log` | core daemon 日志 |
| `~/.mini/logs/tui.log` | TUI 日志 |
| `~/.mini/traces/daemon.jsonl` | IPC、event、LLM trace |
| `~/.mini/sessions/` | session、run events、notes、summary |
| `~/.mini/policy.toml` | 持久化权限策略 |
| `~/.mini/context.md` | 全局上下文 |
| `.mini/context.md` | 当前项目上下文 |

更完整的配置示例、故障排查和日常操作见 [RUNBOOK.md](./RUNBOOK.md)。

## 开发与验证

MiniClaude 使用 `src/` 布局、Hatchling 构建、Ruff + mypy + pytest 工具链。

```bash
uv sync
uv run ruff check src tests scripts
uv run mypy src
uv run pytest tests/unit -v
```

改动协议模型后重新生成协议文档：

```bash
make docs
```

提交前建议运行：

```bash
make verify-s0
```

`make verify-s0` 会执行依赖同步、lint、类型检查、单元测试、ping 集成测试，以及
`WIRE_PROTOCOL.md` 同源检查。

## 文档地图

- [ROADMAP.md](./ROADMAP.md)：S8–S11 的技术方向、范围边界与评测门槛。
- [RUNBOOK.md](./RUNBOOK.md)：日常操作、配置、日志、开发命令和故障排查。
- [WIRE_PROTOCOL.md](./WIRE_PROTOCOL.md)：由代码生成的 IPC 协议文档。
- [AGENT.md](./AGENT.md)：给 Codex/agent 的仓库工作指南。
- [CLAUDE.md](./CLAUDE.md)：给 Claude Code 的仓库工作指南。
- [LICENSE](./LICENSE)：MIT License。
