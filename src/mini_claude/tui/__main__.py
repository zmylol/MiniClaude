from __future__ import annotations

import argparse

from mini_claude.core.config import get_config
from mini_claude.tui.app import MiniTuiApp


# mini-tui 入口：解析 --replay 参数后启动 TUI 应用
def main() -> None:
    parser = argparse.ArgumentParser(prog="mini-tui", description="MiniClaude TUI")
    parser.add_argument(
        "--replay",
        metavar="RUN_ID",
        help="Replay events from a past run on connect",
    )
    args = parser.parse_args()

    config = get_config()
    app = MiniTuiApp(config.host, config.port, replay_run_id=args.replay)
    app.run()


if __name__ == "__main__":
    main()
