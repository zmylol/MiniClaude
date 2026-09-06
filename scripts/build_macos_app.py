#!/usr/bin/env python3
"""为当前 checkout 生成可双击启动的本机 macOS 应用入口。"""

from __future__ import annotations

import importlib
import importlib.util
import plistlib
import shlex
import struct
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "dev.miniclaude.desktop"


# 使用系统 AppKit 绘制终端图标，将 PNG 封装为单一高清 icns 图块。
def create_icon() -> bytes:
    appkit = importlib.import_module("AppKit")
    bitmap = appkit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, 1024, 1024, 8, 4, True, False, appkit.NSDeviceRGBColorSpace, 0, 0
    )
    context = appkit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap)
    appkit.NSGraphicsContext.saveGraphicsState()
    appkit.NSGraphicsContext.setCurrentContext_(context)
    try:
        appkit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.09, 0.10, 0.095, 1).set()
        background = appkit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            ((32, 32), (960, 960)), 220, 220
        )
        background.fill()
        appkit.NSColor.whiteColor().set()
        terminal = appkit.NSBezierPath.bezierPath()
        terminal.setLineWidth_(62)
        terminal.setLineCapStyle_(appkit.NSRoundLineCapStyle)
        terminal.setLineJoinStyle_(appkit.NSRoundLineJoinStyle)
        terminal.moveToPoint_((260, 674))
        terminal.lineToPoint_((410, 512))
        terminal.lineToPoint_((260, 350))
        terminal.moveToPoint_((554, 350))
        terminal.lineToPoint_((768, 350))
        terminal.stroke()
    finally:
        appkit.NSGraphicsContext.restoreGraphicsState()
    png = bytes(bitmap.representationUsingType_properties_(appkit.NSBitmapImageFileTypePNG, {}))
    chunk = b"ic10" + struct.pack(">I", len(png) + 8) + png
    return b"icns" + struct.pack(">I", len(chunk) + 8) + chunk


# 只更新本脚本生成的固定文件，拒绝覆盖未知应用包或符号链接目标。
def build_app() -> Path:
    if sys.platform != "darwin":
        raise SystemExit("此启动器仅用于 macOS。")
    python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not python.is_file():
        raise SystemExit("未找到项目虚拟环境，请先在项目根目录运行 uv sync --extra desktop。")

    bundle = PROJECT_ROOT / "dist" / "MiniClaude.app"
    contents = bundle / "Contents"
    executable = contents / "MacOS" / "MiniClaude"
    icon = contents / "Resources" / "MiniClaude.icns"
    info_path = contents / "Info.plist"
    directories = [bundle.parent, bundle, contents, executable.parent, icon.parent]
    for path in [*directories, executable, icon, info_path]:
        if path.is_symlink():
            raise SystemExit(f"拒绝写入符号链接：{path}")
    if bundle.exists():
        try:
            previous = plistlib.loads(info_path.read_bytes())
        except (OSError, plistlib.InvalidFileException) as error:
            raise SystemExit(f"目标已存在且不是已知 MiniClaude 启动器：{bundle}") from error
        if previous.get("CFBundleIdentifier") != BUNDLE_ID or not previous.get(
            "MiniClaudeLocalLauncher"
        ):
            raise SystemExit(f"拒绝覆盖未知应用包：{bundle}")

    if importlib.util.find_spec("AppKit") is None:
        raise SystemExit("缺少 macOS 桌面依赖，请运行 uv sync --extra desktop 后重试。")
    project_arg = shlex.quote(str(PROJECT_ROOT))
    launcher = (
        '#!/bin/sh\nset -eu\n'
        'mkdir -p "$HOME/.mini/desktop"\n'
        'exec >> "$HOME/.mini/desktop/launcher.log" 2>&1\n'
        f"cd {project_arg}\n"
        f"exec {shlex.quote(str(python))} -m mini_claude.desktop\n"
    )
    icon_data = create_icon()
    version = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"]["version"]
    info = plistlib.dumps(
        {
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleName": "MiniClaude",
            "CFBundleDisplayName": "MiniClaude",
            "CFBundleExecutable": "MiniClaude",
            "CFBundleIconFile": "MiniClaude.icns",
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1",
            "CFBundleShortVersionString": version,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": True,
            "LSMultipleInstancesProhibited": True,
            "MiniClaudeLocalLauncher": True,
        }
    )
    for directory in directories:
        directory.mkdir(exist_ok=True)
    info_path.write_bytes(info)
    executable.write_text(launcher, encoding="utf-8")
    executable.chmod(0o755)
    icon.write_bytes(icon_data)
    return bundle


if __name__ == "__main__":
    print(f"已生成：{build_app()}")
    print("可在 Finder 中双击启动；此本机启动器依赖当前项目目录及 .venv。")
