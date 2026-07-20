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

# ── Self-identify (RDP client → real user mapping) ─────────────────────────
IDENTIFY_CHECK_DELAY_S    = 60          # don't pop up during login storm
IDENTIFY_SKIP_TTL_S       = 24 * 3600   # how long "Skip" mutes the popup
IDENTIFY_SKIP_FLAG_FILE   = "identify-skip-until.txt"
_identify_state = {"checked": False, "mapped": None, "client_machine": None}
_identify_lock  = None   # threading.Lock instance, created in main()

# ── Auto-update ────────────────────────────────────────────────────────────
CURRENT_VERSION = "1.8.6"   # bumped on every release; ground truth for comparisons
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


def _local_state_dir() -> Path:
    """Per-CLIENTNAME local state, mirrors collector.py's _state_path layout."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    root = Path(base) / APP_NAME
    clientname = (os.environ.get("CLIENTNAME") or "").strip()
    if clientname and clientname.lower() != "console":
        # Slug for the dir name; mirrors collector's _safe_dirname
        slug = "".join(
            ch if ch.isalnum() or ch in ("-", "_", ".") else "_"
            for ch in clientname[:64]
        ) or "default"
        out = root / "clients" / slug
    else:
        out = root
    out.mkdir(parents=True, exist_ok=True)
    return out


def _load_collector_config() -> Optional[Dict[str, Any]]:
    """Read the same config.json the collector uses (next to the exe)."""
    cfg_path = install_dir() / "config.json"
    if not cfg_path.is_file():
        return None
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _current_client_machine() -> Optional[str]:
    """The CLIENTNAME the collector identifies by. None on physical
    (non-RDP) installs where we use getpass.getuser instead -- those
    users don't need the identify popup."""
    cn = (os.environ.get("CLIENTNAME") or "").strip()
    if not cn or cn.lower() == "console":
        return None
    return cn


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


def on_check_usage(icon, item):
    exe = collector_path()
    if not exe.exists():
        return
    try:
        subprocess.Popen(
            [str(exe), "usage"],
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


def _skip_flag_until() -> Optional[float]:
    """Read the snooze timestamp set when user clicks Skip in the popup.
    Returns the unix epoch seconds until which we should stay quiet."""
    f = _local_state_dir() / IDENTIFY_SKIP_FLAG_FILE
    if not f.is_file():
        return None
    try:
        return float(f.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _write_skip_until(ts: float) -> None:
    try:
        f = _local_state_dir() / IDENTIFY_SKIP_FLAG_FILE
        f.write_text(str(ts), encoding="utf-8")
    except Exception:
        pass


def _ingest_url(path: str) -> Optional[str]:
    cfg = _load_collector_config()
    if not cfg or not cfg.get("server_url"):
        return None
    return cfg["server_url"].rstrip("/") + path


def _ingest_headers() -> Optional[Dict[str, str]]:
    cfg = _load_collector_config()
    if not cfg or not cfg.get("ingest_token"):
        return None
    return {
        "X-Ingest-Token": cfg["ingest_token"],
        "Content-Type":   "application/json",
        "User-Agent":     f"{APP_NAME}-Tray/{CURRENT_VERSION}",
    }


def _check_identify_status() -> Optional[bool]:
    """Returns True if mapped, False if not, None if we couldn't tell
    (no network, missing config, etc.) -- in which case we just stay
    quiet and try again next launch."""
    client_machine = _current_client_machine()
    if not client_machine:
        return True   # non-RDP machine -- nothing to identify
    url     = _ingest_url(f"/api/identify?client_machine={client_machine}")
    headers = _ingest_headers()
    if not url or not headers:
        return None
    try:
        req = urllib.request.Request(url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
            return bool(data.get("mapped"))
    except Exception:
        return None


def _submit_identity(os_username: str, display_name: str, email: str) -> tuple[bool, str]:
    """POST to /api/identify. Returns (ok, message)."""
    client_machine = _current_client_machine()
    if not client_machine:
        return (False, "This isn't an RDP session — no CLIENTNAME to map.")
    url     = _ingest_url("/api/identify")
    headers = _ingest_headers()
    if not url or not headers:
        return (False, "Server URL or ingest token missing from config.json.")
    body = json.dumps({
        "client_machine": client_machine,
        "os_username":    os_username,
        "display_name":   display_name or None,
        "email":          email or None,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return (True, "Identity confirmed.")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
            return (False, err.get("error") or f"HTTP {e.code}")
        except Exception:
            return (False, f"HTTP {e.code}")
    except Exception as e:
        return (False, f"Network error: {e}")


def show_identify_dialog(forced_open=False) -> None:
    """Open a tkinter modal asking the user for their first_name.last_name.

    Called either from the identity-check background thread (when unmapped)
    or directly from the tray's "Identify this machine" menu item.
    """
    import re
    import tkinter as tk
    from tkinter import ttk, messagebox

    client_machine = _current_client_machine()
    if not client_machine:
        return

    # Snooze check (only honor when the popup is auto-triggered, not when
    # the user explicitly clicked the menu item)
    if not forced_open:
        skip_until = _skip_flag_until()
        if skip_until and time.time() < skip_until:
            return

    root = tk.Tk()
    root.title(f"{DISPLAY_NAME} — Identify your machine")
    root.geometry("460x340")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    pad = 14
    frm = tk.Frame(root, padx=pad, pady=pad)
    frm.pack(fill="both", expand=True)

    tk.Label(frm, text="Welcome — please identify yourself",
             font=("Segoe UI", 12, "bold"), fg="#d97757").pack(anchor="w")
    tk.Label(frm,
             text=f"This machine: {client_machine}\n\n"
                  f"To attribute your Claude Code usage correctly, "
                  f"enter your name below in first_name.last_name format.",
             justify="left", wraplength=420).pack(anchor="w", pady=(8, 12))

    # Inputs
    grid = tk.Frame(frm)
    grid.pack(fill="x")
    grid.grid_columnconfigure(1, weight=1)

    tk.Label(grid, text="Username *", anchor="e").grid(row=0, column=0, sticky="e", padx=(0, 8), pady=4)
    username_var = tk.StringVar()
    user_entry = tk.Entry(grid, textvariable=username_var)
    user_entry.grid(row=0, column=1, sticky="ew", pady=4)
    user_entry.focus()

    tk.Label(grid, text="(e.g. samir.tak)", fg="#888",
             font=("Segoe UI", 8)).grid(row=1, column=1, sticky="w")

    tk.Label(grid, text="Display name", anchor="e").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=(8, 4))
    display_var = tk.StringVar()
    tk.Entry(grid, textvariable=display_var).grid(row=2, column=1, sticky="ew", pady=(8, 4))
    tk.Label(grid, text="optional (e.g. Samir Tak)", fg="#888",
             font=("Segoe UI", 8)).grid(row=3, column=1, sticky="w")

    tk.Label(grid, text="Email", anchor="e").grid(row=4, column=0, sticky="e", padx=(0, 8), pady=(8, 4))
    email_var = tk.StringVar()
    tk.Entry(grid, textvariable=email_var).grid(row=4, column=1, sticky="ew", pady=(8, 4))
    tk.Label(grid, text="optional", fg="#888",
             font=("Segoe UI", 8)).grid(row=5, column=1, sticky="w")

    # Buttons
    btns = tk.Frame(frm)
    btns.pack(fill="x", pady=(16, 0))

    def on_confirm():
        username = username_var.get().strip().lower()
        if not re.match(r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$", username):
            messagebox.showerror("Invalid format",
                "Username must be in first_name.last_name format\n"
                "(lowercase letters, digits, hyphens, underscores; "
                "at least one period).",
                parent=root)
            return
        confirm_btn.config(state="disabled", text="Submitting…")
        root.update_idletasks()
        ok, message = _submit_identity(username, display_var.get().strip(), email_var.get().strip())
        if ok:
            messagebox.showinfo("Thanks!",
                f"This machine is now mapped to {username}. "
                f"Your past and future Claude Code activity will be attributed to you.",
                parent=root)
            _identify_state["mapped"] = True
            if _icon_ref is not None:
                try:
                    _icon_ref.menu = _build_menu()
                    _icon_ref.update_menu()
                except Exception:
                    pass
            root.destroy()
        else:
            messagebox.showerror("Couldn't save", message, parent=root)
            confirm_btn.config(state="normal", text="Confirm")

    def on_skip():
        _write_skip_until(time.time() + IDENTIFY_SKIP_TTL_S)
        root.destroy()

    tk.Button(btns, text="Skip for now", command=on_skip, width=14).pack(side="left")
    confirm_btn = tk.Button(btns, text="Confirm",   command=on_confirm,
                             width=14, bg="#d97757", fg="white",
                             activebackground="#c66647", activeforeground="white")
    confirm_btn.pack(side="right")

    # Keyboard shortcuts
    root.bind("<Return>", lambda e: on_confirm())
    root.bind("<Escape>", lambda e: on_skip())

    root.mainloop()


def _identify_check_loop():
    """Background thread: after IDENTIFY_CHECK_DELAY_S, query the server
    once. If unmapped (and not snoozed), surface the dialog. The menu
    item appears whether or not the popup was shown, so the user can
    always re-open it later."""
    time.sleep(IDENTIFY_CHECK_DELAY_S)
    client_machine = _current_client_machine()
    if not client_machine:
        _identify_state["mapped"] = True   # treat non-RDP as "no need"
        return
    _identify_state["client_machine"] = client_machine

    mapped = _check_identify_status()
    if mapped is None:
        return    # silent retry next launch
    _identify_state["checked"] = True
    _identify_state["mapped"]  = mapped

    if not mapped:
        # Rebuild menu so the "Identify this machine" item shows up
        if _icon_ref is not None:
            try:
                _icon_ref.menu = _build_menu()
                _icon_ref.update_menu()
            except Exception:
                pass
        # Auto-open the dialog if user hasn't snoozed it
        show_identify_dialog(forced_open=False)


def on_identify_click(icon, item):
    """User clicked the 'Identify this machine' menu item -- open the
    dialog even if they previously snoozed."""
    show_identify_dialog(forced_open=True)


def _build_menu() -> pystray.Menu:
    """Build the right-click menu. Done as a function so we can rebuild it
    when update or identify state changes."""
    items = [
        pystray.MenuItem("View log",            on_view_log,         default=True),
        pystray.MenuItem("Run push now",        on_push_now),
        pystray.MenuItem("Check usage now",     on_check_usage),
        pystray.MenuItem("Open install folder", on_open_install_dir),
        pystray.MenuItem("Edit config",         on_edit_config),
    ]
    # "Identify this machine" — only shown on RDP machines that we know
    # are unmapped. Always re-openable via click even if user previously
    # snoozed the auto popup.
    if _identify_state.get("checked") and _identify_state.get("mapped") is False:
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem(
            "⚠ Identify this machine", on_identify_click,
        ))
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

    # Self-identify check: 60s after launch, asks the server if this
    # CLIENTNAME is already mapped to a user. If not (and snooze not
    # active), opens a tkinter popup asking the user to confirm.
    threading.Thread(
        target=_identify_check_loop, daemon=True, name="identify-checker",
    ).start()

    icon.run()


if __name__ == "__main__":
    main()
