from __future__ import annotations

from pathlib import Path

import pytest

from mini_claude.core.config import get_config


def _write_env(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# .env 中的值被加载到 config
def test_dotenv_base_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    _write_env(env_file, "MINI_PORT=9999\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINI_PORT", raising=False)

    cfg = get_config()

    assert cfg.port == 9999


# 系统环境变量优先级高于 .env 中的值
def test_system_env_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    _write_env(env_file, "MINI_PORT=9999\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINI_PORT", "8888")

    cfg = get_config()

    assert cfg.port == 8888


# .env 不存在时静默跳过，使用内建默认值
def test_missing_env_file_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINI_PORT", raising=False)

    cfg = get_config()

    assert cfg.port == 7437


# .env 中设置的 MINI_CONFIG 能影响 TOML 文件加载路径
def test_dotenv_before_toml_mini_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toml_path = tmp_path / "custom.toml"
    toml_path.write_bytes(b'[core]\nport = 5555\n')

    env_file = tmp_path / ".env"
    _write_env(env_file, f"MINI_CONFIG={toml_path}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINI_CONFIG", raising=False)
    monkeypatch.delenv("MINI_PORT", raising=False)

    cfg = get_config()

    assert cfg.port == 5555


# 同一变量同时存在于默认值/TOML/.env/系统环境变量时，验证最终值为最高优先级来源
def test_priority_chain_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 默认值：7437
    # TOML：6000
    # .env：7000
    # 系统环境变量：8000（最高）
    toml_path = tmp_path / "mini.toml"
    toml_path.write_bytes(b'[core]\nport = 6000\n')

    env_file = tmp_path / ".env"
    _write_env(env_file, "MINI_PORT=7000\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINI_CONFIG", str(toml_path))
    monkeypatch.setenv("MINI_PORT", "8000")

    cfg = get_config()

    assert cfg.port == 8000
