"""Team Activity collector.

Runs on an admin's machine (where a valid claude.ai Cookie header lives) and
fetches claude.ai's per-user admin analytics for each configured organization,
then pushes the result to the dashboard server's /api/ingest endpoint. The
Vercel server can't call claude.ai itself (Cloudflare blocks its datacenter
IP), so all collection happens here and only the parsed rows are uploaded.

Reads `analytics_orgs` from config.json — a list of:
    {"org": "<org-uuid>", "org_name": "<label>", "cookie": "<full Cookie header>"}

Designed to run DAILY via a Windows Scheduled Task (ClaudeTeamActivityDaily).
Each run collects ONE calendar day using the window [day, day+1) (so the numbers
are that day alone) and stores it under that day's snapshot_date. Default day =
yesterday (the endpoint 400s on today, and only a completed day has settled
data). `team-activity --backfill 30` collects the last 30 days one by one;
`--reset` wipes stored data first.

Imported by collector.py for the `team-activity` subcommand. Stdlib-only.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError


_DEVICE = None


def _device() -> dict:
    """This machine's identity, sent with every team-activity push so the
    dashboard can show which device collected an org's data. Cached."""
    global _DEVICE
    if _DEVICE is None:
        try:
            import getpass
            user = (os.environ.get("USERNAME") or getpass.getuser() or "").strip()
        except Exception:
            user = ""
        try:
            host = socket.gethostname()
        except Exception:
            host = ""
        _DEVICE = {"source_host": host or None, "os_user": user or None}
    return _DEVICE

# Reuse the scheduled-task trigger builder + no-window flag from desktop_usage.
from desktop_usage import _usage_trigger, _NO_WINDOW


CLAUDE_AI_BASE = "https://claude.ai"
ACTIVITY_EP = "/api/organizations/{org}/analytics/activity/users"
MEMBERS_EP = "/api/organizations/{org}/members"
TASK_NAME = "ClaudeTeamActivityDaily"
HTTP_TIMEOUT = 30
PAGE_SIZE = 50
MAX_PAGES = 1000         # safety cap only; real stop is an empty members array
# Which single day a plain daily run collects, as days back from today.
# 1 = yesterday. Must be >= 1: the endpoint 400s on today/future and only a
# completed day has settled data. Overridable via config "analytics_days_back"
# or the --date CLI flag.
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

def _fetch_page(org: str, cookie: str, start_date: str, end_date: str,
                page: int) -> dict:
    """GET one page of the activity endpoint. Raises HTTPError/URLError/ValueError.

    The window is [start_date, end_date). For a single calendar day D, pass
    start_date=D and end_date=D+1 -> the response's data_as_of == D."""
    qs = (f"?page={page}&page_size={PAGE_SIZE}&sort=chats&order=desc"
          f"&start_date={start_date}&end_date={end_date}")
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


def fetch_all_members(org: str, cookie: str, start_date: str,
                      end_date: str) -> List[dict]:
    """Page through the activity endpoint for the window [start_date, end_date),
    incrementing `page` (page_size=50, sort=chats, order=desc), and return every
    member object.

    Stop condition: keep going until claude.ai returns an empty members array
    (`"members": []`). Two extra guards, neither of which can end collection
    early while real users are still coming back:
      • MAX_PAGES — a hard ceiling so a misbehaving endpoint can't loop forever.
      • no-new-members — if a page returns only users we've already collected
        (some APIs clamp an out-of-range page to the last page and keep
        re-serving it), stop instead of looping on duplicates.
    """
    members: List[dict] = []
    seen: set = set()
    for page in range(1, MAX_PAGES + 1):
        data = _fetch_page(org, cookie, start_date, end_date, page)
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


def fetch_roster(org: str, cookie: str) -> List[dict]:
    """GET the full member roster (/members) -> every seat, including members
    who have never been active (no activity rows). Each entry:
    {uuid, email, name, role, seat_tier, created_at}."""
    url = CLAUDE_AI_BASE + MEMBERS_EP.format(org=org)
    req = urlrequest.Request(url, headers={
        "Cookie": cookie, "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://claude.ai/",
    })
    with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read()
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("non-JSON /members response (Cloudflare challenge?)")
    out = []
    for m in (data or []):
        if not isinstance(m, dict):
            continue
        acc = m.get("account") or {}
        out.append({
            "uuid":       acc.get("uuid"),
            "email":      acc.get("email_address"),
            "name":       acc.get("full_name"),
            "role":       m.get("role"),
            "seat_tier":  m.get("seat_tier"),
            "created_at": m.get("created_at"),
        })
    return out


def push_roster(cfg: Dict, entry: dict, no_push: bool = False) -> bool:
    """Fetch + push the member roster for one org (kind:team_roster)."""
    org = (entry.get("org") or "").strip()
    org_name = entry.get("org_name") or org
    cookie = entry.get("cookie") or ""
    try:
        roster = fetch_roster(org, cookie)
        log(f"  - {org_name} roster: {len(roster)} members")
    except Exception as exc:  # noqa: BLE001
        log(f"  - {org_name} roster: FAILED -- {_classify_error(exc)}",
            level="WARN")
        return False
    dev = _device()
    payload = {
        "kind":        "team_roster",
        "org":         org,
        "org_name":    org_name,
        "source_host": dev["source_host"],
        "os_user":     dev["os_user"],
        "roster":      roster,
    }
    if no_push:
        log(f"      (--no-push) would upload roster of {len(roster)}")
        return True
    pushed, msg = _push(cfg, payload)
    if not pushed:
        log(f"      roster push FAILED -- {msg}", level="ERROR")
    return pushed


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

def _valid_orgs(cfg: Dict) -> List[dict]:
    orgs = cfg.get("analytics_orgs") or []
    out = []
    for entry in orgs:
        org = (entry.get("org") or "").strip()
        cookie = entry.get("cookie") or ""
        if org and cookie:
            out.append(entry)
        else:
            log(f"  - skipping malformed org entry (need org + cookie): "
                f"{entry.get('org_name') or org!r}", level="WARN")
    return out


def _collect_one_day(cfg: Dict, entry: dict, day: date,
                     no_push: bool) -> bool:
    """Collect + push a single calendar `day` for one org. Window is
    [day, day+1) so the numbers are that day's activity alone. snapshot_date =
    day. Returns True on success."""
    org = (entry.get("org") or "").strip()
    org_name = entry.get("org_name") or org
    cookie = entry.get("cookie") or ""
    start = day.isoformat()
    end = (day + timedelta(days=1)).isoformat()

    ok, err, members = True, None, []
    try:
        members = fetch_all_members(org, cookie, start, end)
        log(f"  - {org_name} {start}: {len(members)} users")
    except Exception as exc:  # noqa: BLE001 - report every failure, keep going
        ok, err = False, _classify_error(exc)
        log(f"  - {org_name} {start}: FAILED -- {err}", level="ERROR")

    dev = _device()
    payload = {
        "kind":          "team_activity",
        "org":           org,
        "org_name":      org_name,
        "snapshot_date": start,
        "ok":            ok,
        "error":         err,
        "source_host":   dev["source_host"],
        "os_user":       dev["os_user"],
        "members":       members,
    }

    if no_push:
        log(f"      (--no-push) would upload {len(members)} rows, ok={ok}")
        return ok
    if not ok:
        # Still push the failure so the dashboard can flag the cookie, but only
        # for a "today"-ish run; for a backfill day, a failure is just skipped.
        _push(cfg, payload)
        return False
    pushed, msg = _push(cfg, payload)
    if not pushed:
        log(f"      push FAILED -- {msg}", level="ERROR")
        return False
    return True


def _default_day(cfg: Dict) -> date:
    """The single day a plain daily run collects: yesterday by default. The
    endpoint 400s on today, so this is clamped to at least 1 day back."""
    days_back = int(cfg.get("analytics_days_back", DEFAULT_DAYS_BACK)
                    or DEFAULT_DAYS_BACK)
    if days_back < 1:
        days_back = 1
    return date.today() - timedelta(days=days_back)


def collect_and_push(cfg: Dict, day: Optional[str] = None,
                     no_push: bool = False) -> int:
    """Collect + push ONE day for every configured org (default: yesterday).
    Returns a process exit code (0 = all OK, 1 = at least one failed)."""
    orgs = _valid_orgs(cfg)
    if not orgs:
        log("team-activity: no analytics_orgs configured -- nothing to do.")
        return 0

    d = date.fromisoformat(day) if day else _default_day(cfg)
    log(f"team-activity START: day={d.isoformat()} orgs={len(orgs)}",
        section=True)
    any_failed = False
    for entry in orgs:
        push_roster(cfg, entry, no_push)   # refresh the seat roster each run
        if not _collect_one_day(cfg, entry, d, no_push):
            any_failed = True
    log(f"team-activity DONE: {'OK' if not any_failed else 'with failures'}",
        section=True)
    return 1 if any_failed else 0


def backfill(cfg: Dict, days: int = 30, no_push: bool = False) -> int:
    """Collect + push each of the last `days` calendar days, one by one, from
    oldest to yesterday (today isn't collectable -- the endpoint 400s on it).
    Each day is stored under its own snapshot_date."""
    orgs = _valid_orgs(cfg)
    if not orgs:
        log("team-activity: no analytics_orgs configured -- nothing to do.")
        return 0
    if days < 1:
        days = 1

    today = date.today()
    start_day = today - timedelta(days=days)   # inclusive
    end_day = today - timedelta(days=1)         # yesterday, inclusive
    total = (end_day - start_day).days + 1
    log(f"team-activity BACKFILL START: {start_day.isoformat()} .. "
        f"{end_day.isoformat()} ({total} days) orgs={len(orgs)}", section=True)

    for entry in orgs:
        push_roster(cfg, entry, no_push)   # refresh the seat roster once

    any_failed = False
    d = start_day
    while d <= end_day:
        for entry in orgs:
            if not _collect_one_day(cfg, entry, d, no_push):
                any_failed = True
        d += timedelta(days=1)

    log(f"team-activity BACKFILL DONE: {'OK' if not any_failed else 'with failures'}",
        section=True)
    return 1 if any_failed else 0


def reset(cfg: Dict, org: Optional[str] = None) -> int:
    """Ask the dashboard to wipe stored team-activity data (all orgs, or one)
    so a backfill can repopulate cleanly."""
    payload = {"kind": "team_activity_reset"}
    if org:
        payload["org"] = org
    if not (cfg.get("server_url") and cfg.get("ingest_token")):
        log("team-activity reset: server_url/ingest_token not in config",
            level="ERROR")
        return 1
    pushed, msg = _push(cfg, payload)
    log(f"team-activity RESET ({org or 'all'}): {msg}",
        level="INFO" if pushed else "ERROR")
    return 0 if pushed else 1


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
