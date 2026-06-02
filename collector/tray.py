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

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Dict, Any

import pystray
from PIL import Image, ImageDraw, ImageFont


# ── Constants ───────────────────────────────────────────────────────────────

APP_NAME      = "ClaudeUsageCollector"
DISPLAY_NAME  = "Claude Code Usage Collector"
COLLECTOR_EXE = "ClaudeUsageCollector.exe"
UNINSTALLER_GLOBS = ("unins000.exe", "unins001.exe", "unins002.exe")

# ── Auto-update ────────────────────────────────────────────────────────────
CURRENT_VERSION = "1.6.0"   # bumped on every release; ground truth for comparisons
RELEASES_API_URL = (
    "https://api.github.com/repos/samirtak-dynatechconsultancy/claude-usage-exe/releases/latest"
)
UPDATE_CHECK_INTERVAL_S = 24 * 3600    # 24 hours
UPDATE_CHECK_DELAY_S    = 30           # first check fires 30s after tray start, not immediately

# Module-global state. None = no update; dict = {version, download_url, notes_url}.
_update_available: Optional[Dict[str, Any]] = None
_update_thread:    Optional[threading.Thread] = None
_icon_ref:         Optional[pystray.Icon] = None


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


# ── Auto-update helpers ────────────────────────────────────────────────────

def is_admin() -> bool:
    """Whether the current process can write to Program Files (and thus run
    the installer silently). On non-admin RDP sessions we surface the
    update as "ask your admin" rather than triggering a doomed UAC prompt.
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _version_tuple(v: str) -> tuple:
    """Parse 'v1.5.0' / '1.5.0' / '1.5.0-beta.1' into a 3-tuple of ints for
    naive lexicographic comparison. Pre-release suffixes are ignored
    (we don't ship pre-releases through this channel)."""
    s = (v or "0").lstrip("vV").strip()
    parts = []
    for chunk in s.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _check_for_update_once() -> Optional[Dict[str, Any]]:
    """Single GitHub poll. Returns update info if a strictly newer release
    is found, otherwise None. Swallows all network errors silently — the
    caller just retries on the next tick."""
    try:
        req = urllib.request.Request(
            RELEASES_API_URL,
            headers={
                "User-Agent": f"{APP_NAME}-Tray/{CURRENT_VERSION}",
                "Accept":     "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError, OSError):
        return None
    except Exception:
        return None

    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return None
    if _version_tuple(tag) <= _version_tuple(CURRENT_VERSION):
        return None

    # Find the installer .exe in the release assets.
    download_url = None
    for asset in (data.get("assets") or []):
        name = (asset.get("name") or "").lower()
        if name.endswith(".exe") and "setup" in name:
            download_url = asset.get("browser_download_url")
            break
    if not download_url:
        return None

    return {
        "version":      tag.lstrip("vV"),
        "tag":          tag,
        "download_url": download_url,
        "notes_url":    data.get("html_url"),
    }


def _update_check_loop():
    """Background thread: poll, sleep, repeat. Sets module-level
    `_update_available` and rebuilds the menu when state changes."""
    global _update_available
    # Wait briefly before the first check so tray startup isn't blocked
    # if the network is slow.
    time.sleep(UPDATE_CHECK_DELAY_S)
    while True:
        try:
            info = _check_for_update_once()
            prev = _update_available
            if info != prev:
                _update_available = info
                # Re-render the menu by reassigning icon.menu (pystray refreshes
                # the next time the user opens the right-click menu).
                if _icon_ref is not None:
                    try:
                        _icon_ref.menu = _build_menu()
                        _icon_ref.update_menu()
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(UPDATE_CHECK_INTERVAL_S)


def on_install_update(icon, item):
    """Download the latest installer to %TEMP% and launch it."""
    info = _update_available
    if not info:
        return

    # Non-admins can see "update available" but can't actually install
    # silently into Program Files. Open the release page in their browser
    # so they at least have a clear next step (ask admin / save the file).
    if not is_admin():
        url = info.get("notes_url") or info.get("download_url")
        try:
            os.startfile(url)
        except Exception:
            pass
        return

    target = Path(os.environ.get("TEMP") or os.environ.get("LOCALAPPDATA") or str(Path.home())) \
             / f"ClaudeUsageCollector-Setup-{info['version']}.exe"

    try:
        urllib.request.urlretrieve(info["download_url"], str(target))
    except Exception:
        return

    # Launch the installer; UAC will fire because installer requires admin.
    # /SP- suppresses the "ready to install" confirmation dialog so it's
    # one prompt (UAC) for the user. The new installer will taskkill our
    # tray + daemon during PrepareToInstall, so we exit cleanly first.
    try:
        subprocess.Popen([str(target), "/SP-"], close_fds=True)
    except Exception:
        return
    icon.stop()


def _build_menu() -> pystray.Menu:
    """Build the right-click menu. Done as a function so we can rebuild it
    when update state changes."""
    items = [
        pystray.MenuItem("View log",            on_view_log,         default=True),
        pystray.MenuItem("Run push now",        on_push_now),
        pystray.MenuItem("Open install folder", on_open_install_dir),
        pystray.MenuItem("Edit config",         on_edit_config),
    ]
    if _update_available:
        items.append(pystray.Menu.SEPARATOR)
        admin_label = f"↻ Update to v{_update_available['version']}"
        non_admin   = f"Update v{_update_available['version']} available — open release page"
        items.append(pystray.MenuItem(
            admin_label if is_admin() else non_admin,
            on_install_update,
        ))
    items.extend([
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Uninstall",           on_uninstall),
        pystray.MenuItem("Exit tray icon",      on_quit),
    ])
    return pystray.Menu(*items)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    global _icon_ref, _update_thread
    icon = pystray.Icon(
        APP_NAME,
        icon=make_icon_image(),
        title=f"{DISPLAY_NAME} v{CURRENT_VERSION}",
        menu=_build_menu(),
    )
    _icon_ref = icon

    # Kick off the background update checker. daemon=True so it doesn't
    # block process exit when the user picks "Exit tray icon".
    _update_thread = threading.Thread(
        target=_update_check_loop, daemon=True, name="update-checker",
    )
    _update_thread.start()

    icon.run()


if __name__ == "__main__":
    main()
