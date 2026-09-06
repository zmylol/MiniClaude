from __future__ import annotations

import json
import os

from dotenv import dotenv_values

from mini_claude.core.config import MiniConfig, get_config

DEFAULT_CONNECTION_ENV = "_MINI_DESKTOP_DEFAULT_CONNECTION"


# 提取应用默认连接供父进程内存使用，调用方不得将此结果传给渲染器或写入项目记录。
def connection_settings() -> dict[str, str]:
    config = prepare_config()
    settings = {key: os.environ[key] for key in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
    ) if key in os.environ}
    settings["MINI_LLM_DEFAULT_MODEL"] = config.llm.default_model
    return settings


# 项目显式连接按整组应用，只有地址而没有自身密钥时禁止借用 shell 或应用默认密钥。
def apply_project_connection() -> None:
    local = dotenv_values(".env", interpolate=False)
    fields = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")
    if not any(key in local for key in fields):
        return
    if any("${" in (local.get(key) or "") for key in fields):
        raise RuntimeError(
            "项目模型连接不能使用 ${...} 环境变量替换，请填写该连接自己的密钥和地址。"
        )
    api_key = local.get("ANTHROPIC_API_KEY")
    base_url = local.get("ANTHROPIC_BASE_URL")
    if not api_key or not api_key.strip():
        raise RuntimeError(
            "项目已设置模型连接，请在该项目 .env 中同时配置自己的 ANTHROPIC_API_KEY；"
            "不会借用其他连接的密钥。"
        )
    if "ANTHROPIC_BASE_URL" in local and (not base_url or not base_url.strip()):
        raise RuntimeError("项目 ANTHROPIC_BASE_URL 为空，请填写地址或移除此项使用默认服务。")
    for key in fields:
        os.environ.pop(key, None)
        if value := local.get(key):
            os.environ[key] = value


# 普通文件夹可复用应用默认模型连接；任何项目自带地址或密钥都禁止混合凭据。
def prepare_config() -> MiniConfig:
    fallback = json.loads(os.environ.pop(DEFAULT_CONNECTION_ENV, "{}"))
    apply_project_connection()
    config = get_config()
    if not any(key in os.environ for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")):
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
            if fallback.get(key):
                os.environ[key] = fallback[key]
        if (
            "MINI_LLM_DEFAULT_MODEL" not in os.environ
            and config.llm.default_model == MiniConfig().llm.default_model
            and fallback.get("MINI_LLM_DEFAULT_MODEL")
        ):
            os.environ["MINI_LLM_DEFAULT_MODEL"] = fallback["MINI_LLM_DEFAULT_MODEL"]
            config.llm.default_model = fallback["MINI_LLM_DEFAULT_MODEL"]
    return config


# 在项目自己的工作目录初始化连接后启动 core，原始默认凭据不会留在额外环境变量中。
def main() -> None:
    prepare_config()
    from mini_claude.core.app import run
    run()


if __name__ == "__main__":
    main()
