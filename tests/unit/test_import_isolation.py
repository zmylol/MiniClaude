from __future__ import annotations

import subprocess
import sys

import pytest


# 功能：Core、工具和客户端模块均可作为全新进程的首个导入，避免测试顺序隐藏循环依赖
# 设计：每个参数创建独立解释器，覆盖此前事件总线、会话与 MCP 之间出现过的导入环
@pytest.mark.parametrize("module", [
    "mini_claude.core.events.bus",
    "mini_claude.core.session.manager",
    "mini_claude.core.subagent.tool",
    "mini_claude.core.mcp.client",
    "mini_claude.core.runner",
    "mini_claude.cli.commands.run",
    "mini_claude.cli.commands.chat",
    "mini_claude.tui.app",
])
def test_entry_modules_import_without_prior_test_initialization(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import importlib, sys; importlib.import_module(sys.argv[1])", module],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
