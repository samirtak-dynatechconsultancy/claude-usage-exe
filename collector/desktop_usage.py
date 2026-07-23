"""Claude Desktop subscription usage reader.

Decrypts the sessionKey cookie from Claude Desktop's Chromium cookie store
(DPAPI + AES-256-GCM), calls the internal usage endpoint, and reports
the 5-hour (session) and 7-day (weekly) utilization percentages.

Imported by collector.py for the `usage` subcommand.
Dependency: pycryptodome (AES-GCM). DPAPI via ctypes (no pywin32).
"""

from __future__ import annotations

import base64
import csv
import ctypes
import ctypes.wintypes
import json
import os
import platform
import re
import shutil
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_AI_BASE = "https://claude.ai"
USAGE_EP = "/api/organizations/{org_uuid}/usage"
ORGS_EP = "/api/organizations"
BOOTSTRAP_EP = "/api/bootstrap"
ACCOUNT_EP = "/api/account"

import glob


def _claude_data_dirs():
    """Candidate Claude Desktop userData dirs, newest-looking first. Covers:
      - standard installer:  %APPDATA%\\Claude
      - Microsoft Store/MSIX: %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude
    Globs the package folder so we don't hardcode the publisher hash
    (e.g. Claude_pzs8sxrjxfjjc) and it survives package updates."""
    dirs = []
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        dirs.append(os.path.join(appdata, "Claude"))
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        dirs.extend(sorted(glob.glob(os.path.join(
            local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude"))))
        dirs.append(os.path.join(local, "Claude"))
    return dirs


def _find_claude_file(*relparts):
    for d in _claude_data_dirs():
        p = os.path.join(d, *relparts)
        if os.path.exists(p):
            return p
    return None


def cookie_db_path():
    return _find_claude_file("Network", "Cookies") or _find_claude_file("Cookies")


def local_state_path():
    return _find_claude_file("Local State")


USAGE_TASK_NAME = "ClaudeUsageDaily"
HTTP_TIMEOUT = 15


# ---------------------------------------------------------------------------
# AES-GCM (lazy import so collector.py can load without pycryptodome
# installed — it only fails if someone actually runs the `usage` command)
# ---------------------------------------------------------------------------

def _aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes,
                     tag: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise ImportError(
            "pycryptodome is required for the 'usage' command.\n"
            "Install it with:  pip install pycryptodome")
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag)


# ---------------------------------------------------------------------------
# DPAPI via ctypes (no pywin32 dependency)
# ---------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi_decrypt(encrypted: bytes) -> bytes:
    blob_in = _DATA_BLOB()
    blob_in.cbData = len(encrypted)
    blob_in.pbData = ctypes.cast(
        ctypes.create_string_buffer(encrypted, len(encrypted)),
        ctypes.POINTER(ctypes.c_ubyte),
    )
    blob_out = _DATA_BLOB()

    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0,
        ctypes.byref(blob_out),
    )
    if not ok:
        code = ctypes.GetLastError()
        raise OSError(
            f"DPAPI CryptUnprotectData failed (0x{code:08x}). "
            "Are you running as the same user who signed into Claude Desktop?")

    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


# ---------------------------------------------------------------------------
# Cookie decryption
# ---------------------------------------------------------------------------

def _get_encryption_key() -> bytes:
    ls = local_state_path()
    if not ls:
        raise FileNotFoundError(
            "Claude Desktop Local State not found (checked %APPDATA%\\Claude and "
            "the Microsoft Store package path). Is Claude Desktop installed & "
            "signed in?")

    with open(ls, "r", encoding="utf-8") as f:
        local_state = json.load(f)

    b64_key = local_state.get("os_crypt", {}).get("encrypted_key")
    if not b64_key:
        raise ValueError("os_crypt.encrypted_key missing from Local State")

    raw = base64.b64decode(b64_key)
    if raw[:5] != b"DPAPI":
        raise ValueError("Unexpected encrypted_key prefix (expected 'DPAPI')")

    return _dpapi_decrypt(raw[5:])


def _decrypt_cookie(encrypted_value: bytes, key: bytes) -> str:
    version = encrypted_value[:3]

    if version == b"v20":
        raise ValueError(
            "v20 (app-bound) encryption detected -- DPAPI alone cannot "
            "unwrap it. Claude Desktop may have updated its cookie "
            "encryption.")

    if version not in (b"v10", b"v11"):
        raise ValueError(f"Unknown cookie encryption version: {version!r}")

    nonce = encrypted_value[3:15]
    ciphertext = encrypted_value[15:-16]
    tag = encrypted_value[-16:]

    plaintext = _aes_gcm_decrypt(key, nonce, ciphertext, tag)
    decoded = plaintext.decode("utf-8", errors="replace")

    # Newer Chromium prepends 32-byte SHA256(host) to the plaintext.
    idx = decoded.find("sk-ant")
    if idx >= 0:
        return decoded[idx:]
    return decoded


# ---------------------------------------------------------------------------
# Cookie retrieval (with VSS fallback for locked DB)
# ---------------------------------------------------------------------------

# Set False to never auto-close Claude (rely on closed-app or admin/VSS only).
ALLOW_CLOSE_CLAUDE = True

# CREATE_NO_WINDOW: keep background subprocess calls (powershell, taskkill,
# esentutl, schtasks) from flashing a console window.
_NO_WINDOW = 0x08000000


def _running_claude_path():
    """Path of a running claude.exe (to relaunch later), or None if not running."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"name='claude.exe'\" "
             "| Select-Object -First 1 -ExpandProperty ExecutablePath"],
            capture_output=True, text=True, timeout=15,
            creationflags=_NO_WINDOW).stdout.strip()
        return out or None
    except Exception:
        return None


def _confirm_restart() -> bool:
    """Ask the user before closing Claude. Returns True only if they click Yes.
    Shown on the interactive desktop; if no UI is available (e.g. session 0) or
    they decline/close it, returns False so we skip rather than disrupt them."""
    try:
        MB_YESNO, MB_ICONQUESTION = 0x4, 0x20
        MB_SYSTEMMODAL, MB_SETFOREGROUND = 0x1000, 0x10000
        IDYES = 6
        r = ctypes.windll.user32.MessageBoxW(
            0,
            "To record your Claude usage, the collector needs to briefly close "
            "and reopen Claude Desktop.\n\n"
            "Save anything you're typing first. Close and reopen Claude now?",
            "Claude Usage Collector",
            MB_YESNO | MB_ICONQUESTION | MB_SYSTEMMODAL | MB_SETFOREGROUND)
        return r == IDYES
    except Exception:
        return False


def _close_claude():
    subprocess.run(["taskkill", "/F", "/IM", "claude.exe"],
                   capture_output=True, creationflags=_NO_WINDOW)
    time.sleep(2)   # let Windows release the cookie DB handle before we copy


def _start_claude(path):
    if path and os.path.exists(path):
        try:
            subprocess.Popen([path])
        except Exception:
            pass


def _copy_cookie_db():
    """Copy the cookie DB. Returns (tmp_path, claude_restart_path).

    claude_restart_path is set ONLY when we had to close Claude to read the DB;
    the caller must relaunch it. Order minimises disruption:
      1. direct copy   -> Claude already closed (no admin, no disruption)
      2. VSS snapshot  -> Claude open + admin  (no disruption)
      3. close/reopen  -> Claude open, no admin (brief force-quit of Claude)
    """
    src = cookie_db_path()
    if not src:
        raise FileNotFoundError(
            "Claude Desktop cookie DB not found (checked %APPDATA%\\Claude and "
            "the Microsoft Store package path). Is Claude Desktop signed in?")

    tmp = tempfile.mktemp(suffix=".db")

    # 1. Direct copy (Claude fully closed).
    try:
        shutil.copy2(src, tmp)
        return tmp, None
    except (PermissionError, OSError):
        pass

    # 2. VSS snapshot (needs admin) -- preferred when elevated, no disruption.
    try:
        r = subprocess.run(
            ["esentutl.exe", "/y", "/vss", src, "/d", tmp],
            capture_output=True, timeout=30, creationflags=_NO_WINDOW,
        )
        if r.returncode == 0 and os.path.exists(tmp):
            return tmp, None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # 3. No admin: ASK the user, then briefly close Claude so the file unlocks,
    #    then relaunch it. We never close Claude without an explicit "Yes".
    if ALLOW_CLOSE_CLAUDE:
        claude_path = _running_claude_path()
        if claude_path:
            if not _confirm_restart():
                raise PermissionError(
                    "User declined the Claude restart -- skipping this run.")
            _close_claude()
            try:
                shutil.copy2(src, tmp)
                return tmp, claude_path
            except (PermissionError, OSError):
                _start_claude(claude_path)   # restore even if the copy failed

    raise PermissionError(
        "Cannot read cookie DB -- Claude Desktop has it locked and it could "
        "not be copied. Quit Claude fully, or run as Administrator.")


def get_session_cookie() -> str:
    """Extract and decrypt the sessionKey cookie from Claude Desktop."""
    key = _get_encryption_key()
    db_copy, restart_path = _copy_cookie_db()
    try:
        # Build a proper file: URI so ?immutable=1 is honoured. A raw Windows
        # path with uri=True makes SQLite treat "...db?immutable=1" as a literal
        # filename -> "unable to open database file".
        db_uri = Path(db_copy).as_uri() + "?immutable=1"
        conn = sqlite3.connect(db_uri, uri=True)
        row = conn.execute(
            "SELECT encrypted_value FROM cookies "
            "WHERE host_key LIKE '%claude.ai%' AND name = 'sessionKey'"
        ).fetchone()
        conn.close()

        if not row:
            raise ValueError(
                "sessionKey cookie not found in Claude Desktop's store.\n"
                "Is Claude Desktop signed in to claude.ai?")

        return _decrypt_cookie(row[0], key)
    finally:
        # Relaunch Claude if we closed it, then clean up the temp copy.
        if restart_path:
            _start_claude(restart_path)
        try:
            os.unlink(db_copy)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _api_get(path: str, cookie: str):
    url = CLAUDE_AI_BASE + path
    req = urlrequest.Request(url, headers={
        "Cookie": f"sessionKey={cookie}",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Accept": "application/json",
    })
    with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def _resolve_org(cookie: str) -> Tuple[str, dict]:
    orgs = _api_get(ORGS_EP, cookie)
    if not orgs:
        raise ValueError("No organizations found for this account.")

    for org in orgs:
        uuid = org.get("uuid")
        if not uuid:
            continue
        try:
            usage = _api_get(USAGE_EP.format(org_uuid=uuid), cookie)
            if usage.get("five_hour", {}).get("utilization") is not None:
                return uuid, org
        except (HTTPError, URLError):
            continue

    return orgs[0].get("uuid"), orgs[0]


def _get_email(cookie: str, org: Optional[dict] = None) -> Optional[str]:
    for ep in (BOOTSTRAP_EP, ACCOUNT_EP):
        try:
            data = _api_get(ep, cookie)
            email = (data.get("email")
                     or data.get("account", {}).get("email"))
            if email:
                return email
        except (HTTPError, URLError, KeyError):
            continue

    if org:
        m = re.search(r"([\w.+-]+@[\w.-]+\.\w+)", org.get("name", ""))
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Core: read usage
# ---------------------------------------------------------------------------

def read_usage(cookie: str) -> Dict:
    """Fetch usage data. Returns a dict with all fields."""
    org_uuid, org = _resolve_org(cookie)
    email = _get_email(cookie, org)
    usage = _api_get(USAGE_EP.format(org_uuid=org_uuid), cookie)

    five = usage.get("five_hour", {})
    seven = usage.get("seven_day", {})

    return {
        "captured_at":         datetime.now(timezone.utc).isoformat(),
        "email":               email,
        "org_id":              org_uuid,
        "session_pct":         five.get("utilization"),
        "weekly_pct":          seven.get("utilization"),
        "five_hour_resets_at": five.get("resets_at"),
        "seven_day_resets_at": seven.get("resets_at"),
        "host":                platform.node(),
        "os_user":             os.getlogin(),
    }


# ---------------------------------------------------------------------------
# Output: console, CSV, Supabase
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "captured_at", "email", "org_id", "session_pct", "weekly_pct",
    "five_hour_resets_at", "seven_day_resets_at", "host", "os_user",
]


def print_usage(data: Dict):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    email = data.get("email") or "unknown"
    org = (data.get("org_id") or "")[:10]
    s5 = data.get("session_pct", "?")
    s7 = data.get("weekly_pct", "?")
    print(f"[{ts}] {email} org {org}... 5h: {s5}% 7d: {s7}%")


def append_csv(data: Dict, path: str):
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(data)


def push_usage(data: Dict, config: Dict):
    """POST usage data to the dashboard server. Non-fatal on failure."""
    server_url = (config.get("server_url") or "").rstrip("/")
    token = config.get("ingest_token") or ""

    if not server_url or not token:
        print("  (Usage push skipped -- server_url/ingest_token not in config)")
        return

    body = json.dumps({
        "email":                data.get("email"),
        "org_id":               data.get("org_id"),
        "session_pct":          data.get("session_pct"),
        "weekly_pct":           data.get("weekly_pct"),
        "five_hour_resets_at":  data.get("five_hour_resets_at"),
        "seven_day_resets_at":  data.get("seven_day_resets_at"),
        "host":                 data.get("host"),
        "os_user":              data.get("os_user"),
    }).encode("utf-8")

    req = urlrequest.Request(
        f"{server_url}/api/ingest", data=body, method="POST",
        headers={
            "X-Ingest-Token": token,
            "Content-Type": "application/json",
        })
    try:
        with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            resp_body = json.loads(resp.read())
            if resp_body.get("ok"):
                print("  Server: pushed OK")
            else:
                print(f"  Server: {resp_body}")
    except Exception as exc:
        print(f"  Usage push failed (non-fatal): {exc}")


# ---------------------------------------------------------------------------
# Task Scheduler
# ---------------------------------------------------------------------------

def _usage_trigger(every: int, unit: str, at_time: str) -> Tuple[str, bool]:
    """Return (PowerShell '$trigger = ...' assignment, wake_from_sleep).

    minutes/hours -> repeat from now indefinitely (no wake, so a short cadence
    doesn't keep waking the PC). days/weeks -> fire at a time of day, wake +
    catch-up. weeks == days*7.
    """
    u = (unit or "days").lower().rstrip("s")
    if u in ("minute", "min", "m"):
        return (f"$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) "
                f"-RepetitionInterval (New-TimeSpan -Minutes {every}) "
                f"-RepetitionDuration (New-TimeSpan -Days 9999)"), False
    if u in ("hour", "hr", "h"):
        return (f"$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) "
                f"-RepetitionInterval (New-TimeSpan -Hours {every}) "
                f"-RepetitionDuration (New-TimeSpan -Days 9999)"), False
    if u in ("week", "wk", "w"):
        return (f"$trigger = New-ScheduledTaskTrigger -Daily "
                f"-DaysInterval {every * 7} -At '{at_time}'"), True
    return (f"$trigger = New-ScheduledTaskTrigger -Daily "
            f"-DaysInterval {every} -At '{at_time}'"), True


def install_task(exe_path: str, every: int = 1, unit: str = "days",
                 at_time: str = "18:00", fleet: bool = False):
    """Register the recurring usage task.

    fleet=False -> runs as the current interactive user (attended install).
    fleet=True  -> runs as BUILTIN\\Users (whoever is logged on) so a silent
                   SYSTEM/Intune install produces a task in the user's context.
                   The read needs no admin: when the cookie DB is locked it ASKS
                   the user (a Yes/No popup) before briefly closing/reopening
                   Claude, so nothing happens without their consent.
    """
    try:
        every = max(1, int(every))
    except (TypeError, ValueError):
        every = 1

    trigger_line, wake = _usage_trigger(every, unit, at_time)
    wake_line = "    -WakeToRun `\n" if wake else ""

    # Run through the hidden VBS launcher (via wscript) so no console window
    # pops up on each collection. Fall back to the exe directly if the VBS
    # isn't alongside it (e.g. running from source).
    vbs = os.path.join(os.path.dirname(exe_path), "run_usage.vbs")
    if os.path.exists(vbs):
        act_exec, act_arg = "wscript.exe", f'"{vbs}"'
    else:
        act_exec, act_arg = f'"{exe_path}"', "usage"

    if fleet:
        principal_line = ("$principal = New-ScheduledTaskPrincipal `\n"
                          "    -GroupId 'BUILTIN\\Users' `\n"
                          "    -RunLevel Highest")
    else:
        principal_line = ("$principal = New-ScheduledTaskPrincipal `\n"
                          '    -UserId "$env:USERDOMAIN\\$env:USERNAME" `\n'
                          "    -LogonType Interactive `\n"
                          "    -RunLevel Highest")

    ps = f"""
$action   = New-ScheduledTaskAction -Execute '{act_exec}' -Argument '{act_arg}'
{trigger_line}
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
{wake_line}    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
{principal_line}
Register-ScheduledTask `
    -TaskName '{USAGE_TASK_NAME}' `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force
"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, creationflags=_NO_WINDOW)
    if r.returncode == 0:
        who = "logged-on user (fleet)" if fleet else "current user"
        print(f"Task '{USAGE_TASK_NAME}' registered: every {every} {unit}, "
              f"{who}, catch-up enabled")
    else:
        print(f"Failed to register task:\n{r.stderr.strip()}")
        sys.exit(1)


def uninstall_task():
    r = subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", USAGE_TASK_NAME],
        capture_output=True, text=True, creationflags=_NO_WINDOW)
    if r.returncode == 0:
        print(f"Task '{USAGE_TASK_NAME}' removed.")
    else:
        print(f"Task not found or removal failed: {r.stderr.strip()}")
