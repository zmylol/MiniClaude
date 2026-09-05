from __future__ import annotations

import argparse

from aiohttp import web

from mini_claude.core.config import get_config
from mini_claude.web.server import create_app


# 启动仅监听本机的浏览器界面，core 的地址和模型沿用项目配置。
def main() -> None:
    parser = argparse.ArgumentParser(prog="mini-web", description="MiniClaude 浏览器界面")
    parser.add_argument("--port", type=int, default=7438, help="Web 端口（默认 7438）")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    web.run_app(create_app(get_config()), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
