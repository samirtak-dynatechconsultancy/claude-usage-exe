"""Team Activity collector.

Runs on an admin's machine (where a valid claude.ai Cookie header lives) and
fetches claude.ai's per-user admin analytics for each configured organization,
then pushes the result to the dashboard server's /api/ingest endpoint. The
Vercel server can't call claude.ai itself (Cloudflare blocks its datacenter
IP), so all collection happens here and only the parsed rows are uploaded.

Reads `analytics_orgs` from config.json — a list of:
    {"org": "<org-uuid>", "org_name": "<label>", "cookie": "<full Cookie header>"}

Designed to run DAILY via a Windows Scheduled Task (ClaudeTeamActivityDaily).
Each run uses start_date = yesterday by default (the endpoint requires a PAST
start_date — start_date = today returns HTTP 400) and tags the result with
today's date; re-running the same day overwrites that day's rows server-side.

Imported by collector.py for the `team-activity` subcommand. Stdlib-only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

# Reuse the scheduled-task trigger builder + no-window flag from desktop_usage.
from desktop_usage import _usage_trigger, _NO_WINDOW


CLAUDE_AI_BASE = "https://claude.ai"
ACTIVITY_EP = "/api/organizations/{org}/analytics/activity/users"
TASK_NAME = "ClaudeTeamActivityDaily"
HTTP_TIMEOUT = 30
PAGE_SIZE = 50
MAX_PAGES = 1000         # safety cap only; real stop is an empty members array
# Default days back for start_date: 1 = yesterday. The analytics endpoint
# returns per-user activity SINCE start_date and rejects a same-day/future
# start_date with HTTP 400, so this must be >= 1. Overridable via config
# "analytics_days_back" or the --start-date CLI flag.
DEFAULT_DAYS_BACK = 1
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# Logging: collector.py injects its log() here so team-activity runs land in
# collector.log (the frozen exe is windowed, so bare print() output is lost).
def _default_log(msg, level="INFO", section=False):
    print(msg)


log = _default_log


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _fetch_page(org: str, cookie: str, start_date: str, page: int) -> dict:
    """GET one page of the activity endpoint. Raises HTTPError/URLError/ValueError."""
    qs = (f"?page={page}&page_size={PAGE_SIZE}&sort=chats&order=desc"
          f"&start_date={start_date}")
    url = CLAUDE_AI_BASE + ACTIVITY_EP.format(org=org) + qs
    req = urlrequest.Request(url, headers={
        "Cookie": cookie,
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://claude.ai/",
    })
    with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read()
    try:
        return json.loads(raw)
    except ValueError:
        # Non-JSON (usually a Cloudflare challenge HTML page).
        raise ValueError("claude.ai returned a non-JSON response "
                         "(likely a Cloudflare challenge -- refresh the cookie)")


def _member_key(m: dict, page: int, idx: int) -> str:
    """Stable identity for a member, used only to detect a page that returns
    nothing new (i.e. the API clamped an out-of-range page to the last one)."""
    for k in ("email", "id", "user_id", "uuid", "account_uuid", "name"):
        v = m.get(k)
        if v:
            return f"{k}:{str(v).strip().lower()}"
    return f"pos:{page}:{idx}"


def fetch_all_members(org: str, cookie: str, start_date: str) -> List[dict]:
    """Page through the activity endpoint, incrementing `page` (page_size=50,
    sort=chats, order=desc), and return every member object.

    Stop condition: keep going until claude.ai returns an empty members array
    (`"members": []`), exactly as requested. Two extra guards, neither of which
    can end collection early while real users are still coming back:
      • MAX_PAGES — a hard ceiling so a misbehaving endpoint can't loop forever.
      • no-new-members — if a page returns only users we've already collected
        (some APIs clamp an out-of-range page to the last page and keep
        re-serving it), stop instead of looping on duplicates.
    """
    members: List[dict] = []
    seen: set = set()
    for page in range(1, MAX_PAGES + 1):
        data = _fetch_page(org, cookie, start_date, page)
        batch = data.get("members", []) if isinstance(data, dict) else []
        if not batch:            # "members": [] -> we're past the last page
            break
        new_in_page = 0
        for idx, m in enumerate(batch):
            if not isinstance(m, dict):
                continue
            key = _member_key(m, page, idx)
            if key in seen:
                continue
            seen.add(key)
            members.append(m)
            new_in_page += 1
        if new_in_page == 0:     # page had only already-seen users -> stop
            break
    return members


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------

def _push(cfg: Dict, payload: Dict) -> Tuple[bool, str]:
    """POST a team_activity payload to /api/ingest. Returns (ok, message)."""
    server_url = (cfg.get("server_url") or "").rstrip("/")
    token = cfg.get("ingest_token") or ""
    if not server_url or not token:
        return False, "server_url/ingest_token not in config"

    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        f"{server_url}/api/ingest", data=body, method="POST",
        headers={"X-Ingest-Token": token, "Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            resp_body = json.loads(resp.read())
        return bool(resp_body.get("ok")), json.dumps(resp_body)
    except HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:200]!r}"
    except (URLError, ValueError, OSError) as e:
        return False, str(e)


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        if exc.code in (401, 403):
            return f"HTTP {exc.code} -- cookie expired or blocked by Cloudflare"
        return f"HTTP {exc.code}"
    if isinstance(exc, URLError):
        return f"network error: {getattr(exc, 'reason', exc)}"
    return str(exc)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def collect_and_push(cfg: Dict, snapshot_date: Optional[str] = None,
                     start_date: Optional[str] = None,
                     no_push: bool = False) -> int:
    """Collect + push team activity for every configured org.

    snapshot_date: the date to TAG the data with (default: today, local).
    start_date:    the analytics window start passed to claude.ai
                   (default: snapshot_date minus `analytics_days_back`).
    Returns a process exit code (0 = all orgs OK, 1 = at least one failed).
    """
    orgs = cfg.get("analytics_orgs") or []
    if not orgs:
        log("team-activity: no analytics_orgs configured -- nothing to do. "
            "Add org+cookie pairs to config.json (see analytics_orgs).")
        return 0

    today = date.today()
    snap = snapshot_date or today.isoformat()
    if start_date:
        window_start = start_date
    else:
        # start_date MUST be a past date -- the endpoint 400s on today/future.
        days_back = int(cfg.get("analytics_days_back", DEFAULT_DAYS_BACK)
                        or DEFAULT_DAYS_BACK)
        if days_back < 1:
            days_back = 1
        window_start = (today - timedelta(days=days_back)).isoformat()

    log(f"team-activity START: snapshot_date={snap} start_date={window_start} "
        f"orgs={len(orgs)}", section=True)

    any_failed = False
    for entry in orgs:
        org = (entry.get("org") or "").strip()
        org_name = entry.get("org_name") or org
        cookie = entry.get("cookie") or ""
        if not org or not cookie:
            log(f"  - skipping malformed org entry (need org + cookie): "
                f"{org_name!r}", level="WARN")
            any_failed = True
            continue

        ok, err, members = True, None, []
        try:
            members = fetch_all_members(org, cookie, window_start)
            log(f"  - {org_name}: fetched {len(members)} users")
        except Exception as exc:  # noqa: BLE001 - report every failure, keep going
            ok, err = False, _classify_error(exc)
            log(f"  - {org_name}: FAILED -- {err}", level="ERROR")

        payload = {
            "kind":          "team_activity",
            "org":           org,
            "org_name":      org_name,
            "snapshot_date": snap,
            "ok":            ok,
            "error":         err,
            "members":       members,
        }

        if no_push:
            log(f"    (--no-push) would upload {len(members)} rows, ok={ok}")
        else:
            pushed, msg = _push(cfg, payload)
            if pushed:
                log(f"    pushed OK ({len(members)} rows, ok={ok})")
            else:
                log(f"    push FAILED -- {msg}", level="ERROR")
                any_failed = True

        if not ok:
            any_failed = True

    log(f"team-activity DONE: {'OK' if not any_failed else 'with failures'}",
        section=True)
    return 1 if any_failed else 0


# ---------------------------------------------------------------------------
# Task Scheduler (mirrors desktop_usage.install_task, own task + action)
# ---------------------------------------------------------------------------

def install_task(exe_path: str, every: int = 1, unit: str = "days",
                 at_time: str = "08:00", fleet: bool = False):
    """Register the recurring team-activity task (default daily at 08:00).

    Runs through run_team_activity.vbs (hidden window) when present, else the
    exe directly. fleet=True registers for BUILTIN\\Users (silent/Intune).
    """
    try:
        every = max(1, int(every))
    except (TypeError, ValueError):
        every = 1

    trigger_line, wake = _usage_trigger(every, unit, at_time)
    wake_line = "    -WakeToRun `\n" if wake else ""

    vbs = os.path.join(os.path.dirname(exe_path), "run_team_activity.vbs")
    if os.path.exists(vbs):
        act_exec, act_arg = "wscript.exe", f'"{vbs}"'
    else:
        act_exec, act_arg = f'"{exe_path}"', "team-activity"

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
    -TaskName '{TASK_NAME}' `
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
        print(f"Task '{TASK_NAME}' registered: every {every} {unit}, "
              f"{who}, catch-up enabled")
    else:
        print(f"Failed to register task:\n{r.stderr.strip()}")
        sys.exit(1)


def uninstall_task():
    r = subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
        capture_output=True, text=True, creationflags=_NO_WINDOW)
    if r.returncode == 0:
        print(f"Task '{TASK_NAME}' removed.")
    else:
        print(f"Task not found or removal failed: {r.stderr.strip()}")
