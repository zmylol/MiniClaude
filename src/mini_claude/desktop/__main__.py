from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from mini_claude.core.config import get_config
from mini_claude.desktop.app import run_desktop
from mini_claude.desktop.core import apply_project_connection


# 从选定项目读取配置，启动没有浏览器地址栏的原生桌面应用。
def main() -> None:
    parser = argparse.ArgumentParser(prog="mini-desktop", description="MiniClaude 桌面应用")
    parser.add_argument("--project", type=Path, help="项目目录（默认恢复上次打开的项目）")
    parser.add_argument("--port", type=int, default=7439, help="本机事件网关端口（默认 7439）")
    parser.add_argument("--storage-path", type=Path, help="桌面存储目录（默认 ~/.mini/desktop）")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    project = args.project or Path.cwd()
    project_selected = True
    if args.project is None:
        storage = (args.storage_path or Path.home() / ".mini" / "desktop").expanduser()
        recent = storage / "projects.json"
        if recent.exists():
            try:
                record = json.loads(recent.read_text(encoding="utf-8"))
                saved = record["current_path"]
                if saved is None and record.get("project_selected") is False:
                    project_selected = False
                if isinstance(saved, str) and Path(saved).is_dir():
                    project = Path(saved)
            except (ValueError, KeyError, TypeError, OSError):
                print("最近项目记录不可读，使用当前目录启动。", file=sys.stderr)
    project = project.expanduser().resolve()
    if not project.is_dir():
        parser.error("--project 必须是存在的项目目录")
    try:
        environment = dict(os.environ)
        os.chdir(project)
        apply_project_connection()
        run_desktop(
            get_config(), project, storage_path=args.storage_path, port=args.port,
            environment=environment,
            project_selected=project_selected,
        )
    except (RuntimeError, OSError) as exc:
        print(f"MiniClaude 桌面启动失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
