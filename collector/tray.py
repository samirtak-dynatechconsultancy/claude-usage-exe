"""tray.py - System tray companion for Claude Code Usage Collector.

Sits in the Windows notification area with a small icon. Right-click menu
gives quick access to the log file, lets you trigger a push immediately,
opens the install folder / config, and runs the uninstaller.

Architecturally separate from collector.py:
  - collector.exe: short-lived, fires from a Scheduled Task every 15 min,
                   does the actual JSONL parsing + HTTPS push, then exits.
  - tray.exe    : always-running, just provides UI. Uses ~10-15 MB RAM.

The tray process does NO data collection or network I/O of its own — it
just shells out to collector.exe when the user clicks "Run push now".

Launched at login via an HKCU Run registry entry created by the installer.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont


# ── Constants ───────────────────────────────────────────────────────────────

APP_NAME      = "ClaudeUsageCollector"
DISPLAY_NAME  = "Claude Code Usage Collector"
COLLECTOR_EXE = "ClaudeUsageCollector.exe"
UNINSTALLER_GLOBS = ("unins000.exe", "unins001.exe", "unins002.exe")


def install_dir() -> Path:
    """Where the .exe (or this .py for dev) lives."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_NAME / "collector.log"


def config_path() -> Path:
    return install_dir() / "config.json"


def collector_path() -> Path:
    return install_dir() / COLLECTOR_EXE


def uninstaller_path() -> Path | None:
    d = install_dir()
    for glob in UNINSTALLER_GLOBS:
        p = d / glob
        if p.exists():
            return p
    return None


# ── Icon ────────────────────────────────────────────────────────────────────

def make_icon_image() -> Image.Image:
    """Generate a 64x64 RGBA icon with a stylized 'C'.

    Drawn at runtime so we don't have to ship a separate .ico file.
    """
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded square background in Anthropic-ish orange
    accent = (217, 119, 87, 255)
    d.rounded_rectangle((2, 2, size - 2, size - 2), radius=12, fill=accent)

    # Big white "C" in the middle. Use a default font (works on every Windows
    # box, no font file needed).
    try:
        font = ImageFont.truetype("arialbd.ttf", 48)
    except Exception:
        font = ImageFont.load_default()

    text = "C"
    bbox = d.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (size - text_w) // 2 - bbox[0]
    y = (size - text_h) // 2 - bbox[1]
    d.text((x, y), text, fill=(255, 255, 255, 255), font=font)
    return img


# ── Menu actions ────────────────────────────────────────────────────────────

CREATE_NO_WINDOW = 0x08000000  # subprocess flag — keep child windowless


def _open(path: Path) -> None:
    try:
        os.startfile(str(path))
    except Exception:
        pass


def on_view_log(icon, item):
    p = log_path()
    if p.exists():
        _open(p)
    else:
        # Log file may not have been created yet — open its folder.
        p.parent.mkdir(parents=True, exist_ok=True)
        _open(p.parent)


def on_push_now(icon, item):
    exe = collector_path()
    if not exe.exists():
        return
    try:
        subprocess.Popen(
            [str(exe), "push"],
            cwd=str(install_dir()),
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )
    except Exception:
        pass


def on_open_install_dir(icon, item):
    _open(install_dir())


def on_edit_config(icon, item):
    cfg = config_path()
    if not cfg.exists():
        return
    try:
        # Notepad is universally available; spawn detached so killing tray
        # doesn't take notepad down with it.
        subprocess.Popen(["notepad.exe", str(cfg)], close_fds=True)
    except Exception:
        pass


def on_uninstall(icon, item):
    u = uninstaller_path()
    if u is None:
        return
    # Launch the uninstaller, then stop the tray. The uninstaller's
    # taskkill /F /IM ClaudeUsageTray.exe step would otherwise race with
    # our exit and might pop a brief error dialog.
    try:
        subprocess.Popen([str(u)], close_fds=True)
    except Exception:
        pass
    icon.stop()


def on_quit(icon, item):
    # User-driven quit only stops the tray icon. The Scheduled Task keeps
    # firing the collector every 15 minutes regardless — uninstall to
    # actually stop all activity.
    icon.stop()


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    menu = pystray.Menu(
        pystray.MenuItem("View log",            on_view_log,         default=True),
        pystray.MenuItem("Run push now",        on_push_now),
        pystray.MenuItem("Open install folder", on_open_install_dir),
        pystray.MenuItem("Edit config",         on_edit_config),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Uninstall",           on_uninstall),
        pystray.MenuItem("Exit tray icon",      on_quit),
    )
    icon = pystray.Icon(
        APP_NAME,
        icon=make_icon_image(),
        title=DISPLAY_NAME,
        menu=menu,
    )
    icon.run()


if __name__ == "__main__":
    main()
