"""collector.py — Claude Code usage collector for end-user machines.

Postgres-only mode (v1.2+): scans Claude Code JSONL transcripts, parses each
user/assistant message into structured data, and POSTs the whole batch to
the dashboard server's /api/ingest endpoint. No Supabase Storage uploads,
no signed-URL dance, no SHA-256 hashing.

Designed to run silently on a Windows Scheduled Task every 15 minutes.
Stdlib-only so PyInstaller produces a small, single-file .exe.

Run manually for testing:

    python collector.py push                # one-shot push of new data
    python collector.py status              # show state + config, no push
    python collector.py reset-state         # forget push history (re-push)

Reads config from (first match wins):
  1.  --config <path>
  2.  COLLECTOR_CONFIG env var
  3.  <exe-dir>\\config.json                (where the installer writes it)
  4.  %APPDATA%\\ClaudeUsageCollector\\config.json
  5.  ./config.json
"""

from __future__ import annotations

import argparse
import atexit
import glob
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError


# ── Constants ───────────────────────────────────────────────────────────────

APP_NAME = "ClaudeUsageCollector"
USER_AGENT = "claude-usage-collector/1.7.3"
DAEMON_SLEEP_SECONDS = 900   # 15 minutes between pushes in daemon mode
DAEMON_LOCK_FILENAME = "daemon.lock"

# WTS_INFO_CLASS values for WTSQuerySessionInformation. Used by the live
# CLIENTNAME check that catches RDP session takeover.
WTS_CURRENT_SERVER  = 0
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTSClientName       = 10
DEFAULT_PROJECTS_DIRS = [
    Path.home() / ".claude" / "projects",
    Path.home() / "Library" / "Developer" / "Xcode" / "CodingAssistant" / "ClaudeAgentConfig" / "projects",
]
# Smaller than v1.x because each record now carries full message content.
# At ~5 KB/turn avg, 50 turns + 100 records ≈ 750 KB payload, well under
# Vercel's 4.5 MB request body limit.
INGEST_BATCH_SIZE = 50
HTTP_TIMEOUT_S = 60


# ── Config loading ──────────────────────────────────────────────────────────

def _exe_dir() -> Path:
    """Directory of the running script or PyInstaller bundle."""
    if getattr(sys, "frozen", False):           # PyInstaller
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _candidate_config_paths(override: Optional[str]) -> List[Path]:
    paths: List[Path] = []
    if override:
        paths.append(Path(override))
    env = os.environ.get("COLLECTOR_CONFIG")
    if env:
        paths.append(Path(env))
    paths.append(_exe_dir() / "config.json")
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / APP_NAME / "config.json")
    paths.append(Path.cwd() / "config.json")
    return paths


def load_config(override: Optional[str] = None) -> Tuple[Dict[str, Any], Path]:
    """Load config.json. Returns (config, path_used)."""
    for p in _candidate_config_paths(override):
        if p.is_file():
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
            cfg.setdefault("server_url",   os.environ.get("SERVER_URL"))
            cfg.setdefault("ingest_token", os.environ.get("INGEST_TOKEN"))
            return cfg, p
    raise FileNotFoundError(
        "No config.json found. Searched:\n  " +
        "\n  ".join(str(x) for x in _candidate_config_paths(override))
    )


# ── Local state ─────────────────────────────────────────────────────────────
# Per-file we track mtime (cheap "did anything change?" check) and lines
# (how many lines we've already sent — so a growing file only sends the
# new tail next time, no duplicate user/assistant message rows on the
# server side). content_hash from v1.x is retained for safety but unused.

def _safe_dirname(s: str) -> str:
    """Slugify a string for use as a Windows directory name."""
    if not s:
        return "default"
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    return ("".join(out) or "default")[:64]


def _state_path() -> Path:
    """Single state file per OS account.

    v1.7.2 partitioned this by CLIENTNAME to handle "multiple RDP clients
    sharing one OS account". v1.7.3 dropped that: Windows lets only one
    client be Active on a given account at a time (the second client
    bumps the first into Disconnected and reattaches to the same session),
    and identity is now read live from the WTS API at each push -- so
    state.json's job is just "what files have we already processed on
    this %LOCALAPPDATA%", which is unambiguously one-per-OS-account.

    Orphaned per-CLIENTNAME state.json files from a v1.7.2 install are
    harmless: ignored on read, and the worst case is a one-time re-push
    of historical turns (server dedupes by message_id).
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    p = Path(base) / APP_NAME / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_state() -> Dict[str, Any]:
    p = _state_path()
    if not p.is_file():
        return {"machine_fp": _make_machine_fp(), "files": {}}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"machine_fp": _make_machine_fp(), "files": {}}


def save_state(state: Dict[str, Any]) -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, p)


def _make_machine_fp() -> str:
    """Stable per-machine identifier (sha256 of hostname + MAC). New MAC after
    a re-image will produce a new fp — intentional, since it's effectively
    a different machine."""
    raw = f"{socket.gethostname()}|{uuid.getnode():012x}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── Identification ──────────────────────────────────────────────────────────

def detect_user() -> Dict[str, str]:
    """Resolve the user identifier for this collector instance.

    Priority order:
      1. CLAUDE_USAGE_USER env var (explicit override)
      2. Live WTSQuerySessionInformation(WTSClientName) -- the currently
         attached RDP client. Reflects session takeover (disconnect/
         reconnect with a different client on the same OS account), which
         the env block does NOT: %CLIENTNAME% in os.environ is a snapshot
         frozen at process spawn and Windows never rewrites it for live
         processes. The whole RDP-shared-account bug came from trusting
         env over the live WTS value.
      3. %CLIENTNAME% env var (fallback for non-Windows builds or weird
         WTS failures; rarely the right answer on Windows).
      4. getpass.getuser() (physical laptop / non-RDP login).
    """
    import getpass

    explicit = (os.environ.get("CLAUDE_USAGE_USER") or "").strip()
    if explicit:
        return {"os_username": explicit, "identity_source": "explicit"}

    live = _wts_get_current_clientname()
    if live and live.lower() not in ("", "console"):
        return {"os_username": live, "identity_source": "clientname_wts"}

    clientname = (os.environ.get("CLIENTNAME") or "").strip()
    if clientname and clientname.lower() != "console":
        return {"os_username": clientname, "identity_source": "clientname_env"}

    return {
        "os_username":     (getpass.getuser() or "unknown").strip(),
        "identity_source": "getuser",
    }


def detect_machine(machine_fp: str) -> Dict[str, Any]:
    """Capture hostname + RDP session info if applicable.

    Prefers the LIVE WTS CLIENTNAME over the env snapshot, for the same
    reason detect_user() does -- the env value is frozen at process spawn
    and doesn't reflect disconnect/reconnect session takeovers.

    is_rdp is true when either:
      - SESSIONNAME starts with 'RDP-' (Windows tags RDP session names this way), or
      - CLIENTNAME (live or env) is set to anything other than 'Console'.
    """
    live_client = _wts_get_current_clientname()
    env_client  = (os.environ.get("CLIENTNAME") or "").strip()
    clientname  = (live_client or env_client or "").strip()

    sessionname = (os.environ.get("SESSIONNAME") or "").strip()
    is_rdp = sessionname.startswith("RDP-") or (
        bool(clientname) and clientname.lower() != "console"
    )

    payload: Dict[str, Any] = {
        "hostname":   socket.gethostname(),
        "os":         f"{sys.platform} {os.environ.get('OS', '')}".strip(),
        "machine_fp": machine_fp,
        "is_rdp":     is_rdp,
    }
    if is_rdp:
        if clientname:
            payload["client_machine"] = clientname
        if sessionname:
            payload["session_id"]     = sessionname
            payload["rdp_session_id"] = sessionname
    return payload


# ── JSONL parsing ───────────────────────────────────────────────────────────

MODEL_PRIORITY = {"opus": 3, "sonnet": 2, "haiku": 1}


def _model_priority(model: Optional[str]) -> int:
    if not model:
        return 0
    m = model.lower()
    for kw, p in MODEL_PRIORITY.items():
        if kw in m:
            return p
    return 0


def _project_name_from_cwd(cwd: str) -> str:
    if not cwd:
        return "unknown"
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else "unknown"


def parse_jsonl_file(
    filepath: str,
    skip_lines: int = 0,
    collect_content: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Parse a JSONL file. Returns (session_metas, turns, records, line_count).

    Session metadata is derived from ALL lines (a full scan is cheap and gives
    us accurate first/last timestamps even if we're only sending the tail).

    Turns and records are emitted only from lines > skip_lines so resumed
    pushes don't re-send already-ingested content. If the file's current
    line count is *less* than skip_lines, the file was truncated/rewritten —
    re-parse everything.

    When collect_content is False (privacy / metadata-only mode), `records`
    is always returned empty. `turns` and session metadata still populate
    so token charts and cost calculations work.
    """
    seen_messages: Dict[str, Dict[str, Any]] = {}
    turns_no_id: List[Dict[str, Any]] = []
    records: List[Dict[str, Any]] = []
    session_meta: Dict[str, Dict[str, Any]] = {}
    line_count = 0

    # First pass: count lines to detect truncation
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for total in f:
                line_count += 1
    except Exception:
        return [], [], [], 0

    if line_count < skip_lines:
        # File shrank — assume rewrite, re-parse everything
        skip_lines = 0

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rtype = record.get("type")
                if rtype not in ("assistant", "user"):
                    continue
                session_id = record.get("sessionId")
                if not session_id:
                    continue

                timestamp = record.get("timestamp", "")
                cwd = record.get("cwd", "")
                git_branch = record.get("gitBranch", "")

                # Session metadata always tracked (cheap, gives accurate ranges).
                if session_id not in session_meta:
                    session_meta[session_id] = {
                        "session_uuid":    session_id,
                        "project_name":    _project_name_from_cwd(cwd),
                        "first_timestamp": timestamp,
                        "last_timestamp":  timestamp,
                        "git_branch":      git_branch,
                        "model":           None,
                    }
                else:
                    meta = session_meta[session_id]
                    if timestamp and (not meta["first_timestamp"] or timestamp < meta["first_timestamp"]):
                        meta["first_timestamp"] = timestamp
                    if timestamp and (not meta["last_timestamp"]  or timestamp > meta["last_timestamp"]):
                        meta["last_timestamp"] = timestamp
                    if git_branch and not meta["git_branch"]:
                        meta["git_branch"] = git_branch

                # Skip turn/record emission for already-processed lines.
                if idx <= skip_lines:
                    if rtype == "assistant":
                        msg = record.get("message", {})
                        model = msg.get("model", "")
                        if model:
                            prev = session_meta[session_id]["model"]
                            if _model_priority(model) > _model_priority(prev):
                                session_meta[session_id]["model"] = model
                            elif not prev:
                                session_meta[session_id]["model"] = model
                    continue

                # Capture per-message content for the messages table — only if
                # the user opted in. When off, we still emit the turn row below
                # so charts/costs work; we just never send what was said.
                msg = record.get("message", {}) or {}
                msg_id = msg.get("id") if rtype == "assistant" else None
                if collect_content:
                    records.append({
                        "session_uuid": session_id,
                        "type":         rtype,
                        "timestamp":    timestamp,
                        "message_uuid": msg_id,
                        "message":      msg,
                    })

                if rtype == "assistant":
                    usage = msg.get("usage", {}) or {}
                    model = msg.get("model", "")
                    message_id = msg_id or ""

                    inp = usage.get("input_tokens", 0) or 0
                    out = usage.get("output_tokens", 0) or 0
                    cr  = usage.get("cache_read_input_tokens", 0) or 0
                    cc  = usage.get("cache_creation_input_tokens", 0) or 0
                    if inp + out + cr + cc == 0:
                        continue   # message with no billed usage, no turn row

                    tool_name = None
                    for item in msg.get("content", []):
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            tool_name = item.get("name")
                            break

                    if model:
                        prev = session_meta[session_id]["model"]
                        if _model_priority(model) > _model_priority(prev):
                            session_meta[session_id]["model"] = model
                        elif not prev:
                            session_meta[session_id]["model"] = model

                    turn = {
                        "session_uuid":          session_id,
                        "message_id":            message_id,
                        "timestamp":             timestamp,
                        "model":                 model,
                        "input_tokens":          inp,
                        "output_tokens":         out,
                        "cache_read_tokens":     cr,
                        "cache_creation_tokens": cc,
                        "tool_name":             tool_name,
                        "cwd":                   cwd,
                    }
                    if message_id:
                        seen_messages[message_id] = turn
                    else:
                        turns_no_id.append(turn)
    except Exception as e:
        log(f"  warning: error reading {filepath}: {e}", level="WARN")

    turns = turns_no_id + list(seen_messages.values())
    return list(session_meta.values()), turns, records, line_count


# ── HTTP ────────────────────────────────────────────────────────────────────

class CollectorError(Exception):
    pass


def _http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None,
          timeout: int = HTTP_TIMEOUT_S) -> Tuple[int, bytes]:
    req = urlrequest.Request(url, data=body, method=method, headers={**headers, "User-Agent": USER_AGENT})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read()


def api_ingest(cfg: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    if len(body) > 4_400_000:
        raise CollectorError(
            f"Ingest payload too large ({len(body)} bytes); reduce INGEST_BATCH_SIZE")
    status, raw = _http(
        "POST",
        cfg["server_url"].rstrip("/") + "/api/ingest",
        headers={
            "Content-Type":   "application/json",
            "X-Ingest-Token": cfg["ingest_token"],
        },
        body=body,
    )
    if status != 200:
        raise CollectorError(f"/api/ingest failed: HTTP {status} {raw[:300]!r}")
    return json.loads(raw)


# ── Logging ─────────────────────────────────────────────────────────────────

_LOG_FILE: Optional[Path] = None

# Push-iteration counter + identity tracking. Lets us number pushes in the log
# so a user reading collector.log in Notepad can ctrl-F "PUSH #42" to jump
# straight to a specific iteration, and tells us when the resolved identity
# changed between pushes (the smoking gun for an RDP session takeover).
_PUSH_SEQ          = 0
_LAST_IDENTITY: Optional[str] = None
_FIRST_WTS_LOG     = True


def _log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / APP_NAME / "collector.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def log(msg: str, level: str = "INFO", section: bool = False) -> None:
    """Append a line to the rotating log.

    Two formats, chosen so the file is scannable in plain Notepad:

      [timestamp] LEVEL  message            <- normal events
      [timestamp] ===== HEADER =====        <- section boundaries (push start/end)

    Quick triage cheatsheet for collector.log:
      - 'ERROR '  -> failures (HTTP, IO, exceptions). Search for these first.
      - 'WARN  '  -> drifts / identity changes / non-fatal issues.
      - '===== '  -> push iteration boundaries; PUSH #N OK/FAIL marks results.
      - '!!!   '  -> identity changed (RDP takeover) -- visually unmissable.
    """
    global _LOG_FILE
    if _LOG_FILE is None:
        _LOG_FILE = _log_path()
    # datetime.utcnow() is deprecated in 3.12+; use timezone-aware now() and
    # format manually to keep the same "...Z" suffix the user sees in logs.
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if section:
        line = f"[{ts}] ===== {msg} ====="
    else:
        # Pad level to width 5 so the message column is aligned regardless of
        # whether the level is INFO/WARN (4 chars) or ERROR (5 chars).
        line = f"[{ts}] {level:<5} {msg}"
    print(line, flush=True)
    try:
        if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > 5_000_000:
            with open(_LOG_FILE, "rb") as f:
                f.seek(-2_000_000, os.SEEK_END)
                tail = f.read()
            with open(_LOG_FILE, "wb") as f:
                f.write(tail)
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Main push loop ──────────────────────────────────────────────────────────

def push(cfg: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """Walk projects dirs, parse new content, POST batches to /api/ingest,
    update local state. Idempotent -- already-processed line ranges are
    skipped via the per-file `lines` counter in state.json.

    Wrapped in a try/finally so each iteration emits balanced "PUSH #N
    START" and "PUSH #N OK/FAIL" section markers in the log; that way a
    user reading collector.log in Notepad can visually pair the start of
    a push with its result.
    """
    global _PUSH_SEQ, _LAST_IDENTITY, _FIRST_WTS_LOG
    _PUSH_SEQ += 1
    push_id = _PUSH_SEQ
    started_at = time.time()
    result: Dict[str, Any] = {
        "files": 0, "turns": 0, "records": 0, "sessions": 0, "batches": 0,
    }

    state = load_state()
    machine_fp = state.get("machine_fp") or _make_machine_fp()
    state["machine_fp"] = machine_fp

    user = detect_user()
    machine = detect_machine(machine_fp)

    identity_str = f"{user['os_username']} ({user['identity_source']})"

    # First push of this daemon's life: dump the raw identity inputs. If the
    # WTS live value differs from the env CLIENTNAME, this line is the
    # confirmation that the v1.7.3 live-WTS fix is doing real work (would
    # have silently misattributed under v1.7.2).
    if _FIRST_WTS_LOG:
        _FIRST_WTS_LOG = False
        live   = _wts_get_current_clientname()
        env_cn = (os.environ.get("CLIENTNAME") or "").strip()
        log(
            f"identity inputs: wts_live='{live or ''}' "
            f"env_clientname='{env_cn}' chosen={identity_str}"
        )

    # Identity change banner. If the daemon survived a disconnect/reconnect
    # with a different RDP client, this is the unmissable line that records
    # it -- search for '!!!' in collector.log to find every takeover.
    if _LAST_IDENTITY is not None and _LAST_IDENTITY != identity_str:
        log(
            f"!!!   identity changed: '{_LAST_IDENTITY}' -> '{identity_str}' "
            f"(likely RDP takeover -- new client attached to the session)",
            level="WARN",
        )
    _LAST_IDENTITY = identity_str

    # Privacy toggle (config.json: "upload_content": true | false).
    # Default true preserves v1.2.x behavior on upgrade. When false, we send
    # turn-level metadata (tokens, model, project, etc.) but NOT a single
    # word of any prompt or response.
    upload_content = bool(cfg.get("upload_content", True))

    log(
        f"PUSH #{push_id} START - identity={identity_str} "
        f"host={machine['hostname']} fp={machine_fp[:8]} "
        f"content_uploads={'on' if upload_content else 'off'}",
        section=True,
    )

    ok = False
    try:
        # ── Discover files ────────────────────────────────────────────────
        projects_dirs = [Path(p) for p in cfg.get("projects_dirs") or []] or DEFAULT_PROJECTS_DIRS
        jsonl_files: List[str] = []
        for d in projects_dirs:
            if not d.exists():
                continue
            jsonl_files.extend(glob.glob(str(d / "**" / "*.jsonl"), recursive=True))
        jsonl_files.sort()
        log(f"  discovered {len(jsonl_files)} jsonl files across {len(projects_dirs)} dirs")

        new_or_changed: List[str] = []
        for fp in jsonl_files:
            try:
                mtime = os.path.getmtime(fp)
            except OSError:
                continue
            rec = state["files"].get(fp)
            if rec and abs(rec.get("mtime", 0) - mtime) < 0.01:
                continue
            new_or_changed.append(fp)
        log(f"  {len(new_or_changed)} files need processing")

        if dry_run:
            result["files"] = len(new_or_changed)
            ok = True
            return {"would_process": len(new_or_changed)}

        sessions_acc: Dict[str, Dict[str, Any]] = {}
        turns_acc: List[Dict[str, Any]] = []
        records_acc: List[Dict[str, Any]] = []
        file_records: List[Dict[str, Any]] = []

        parsed_files = 0
        new_turn_count = 0
        new_record_count = 0

        for fp in new_or_changed:
            try:
                prev = state["files"].get(fp) or {}
                skip_lines = int(prev.get("lines") or 0)

                metas, turns, records, lines = parse_jsonl_file(
                    fp, skip_lines=skip_lines, collect_content=upload_content,
                )
                parsed_files += 1
                new_turn_count   += len(turns)
                new_record_count += len(records)

                for m in metas:
                    ex = sessions_acc.get(m["session_uuid"])
                    if not ex:
                        sessions_acc[m["session_uuid"]] = m
                    else:
                        if m["first_timestamp"] and (not ex["first_timestamp"] or m["first_timestamp"] < ex["first_timestamp"]):
                            ex["first_timestamp"] = m["first_timestamp"]
                        if m["last_timestamp"] and (not ex["last_timestamp"] or m["last_timestamp"] > ex["last_timestamp"]):
                            ex["last_timestamp"] = m["last_timestamp"]
                        if _model_priority(m["model"]) > _model_priority(ex["model"]):
                            ex["model"] = m["model"]

                turns_acc.extend(turns)
                records_acc.extend(records)
                file_records.append({
                    "path":  fp,
                    "mtime": os.path.getmtime(fp),
                    "lines": lines,
                })

                # Pre-stage state update; only commit to disk after successful
                # ingest of the batches containing this file's data.
                state["files"][fp] = {
                    "mtime": os.path.getmtime(fp),
                    "lines": lines,
                }
            except Exception as e:
                log(f"  error processing {fp}: {e}", level="ERROR")
                log(traceback.format_exc(), level="ERROR")
                continue

        log(f"  parsed: {parsed_files} files, {new_turn_count} new turns, {new_record_count} new records")

        if not turns_acc and not records_acc and not sessions_acc:
            log("  nothing to ingest")
            save_state(state)
            result.update({"files": parsed_files})
            ok = True
            return result

        # ── POST in batches ────────────────────────────────────────────────
        sessions_list = list(sessions_acc.values())
        total_batches = max(
            1,
            (len(turns_acc)   + INGEST_BATCH_SIZE - 1) // INGEST_BATCH_SIZE,
            (len(records_acc) + INGEST_BATCH_SIZE - 1) // INGEST_BATCH_SIZE,
        )
        batches_sent = 0

        for i in range(total_batches):
            t_start = i * INGEST_BATCH_SIZE
            r_start = i * INGEST_BATCH_SIZE
            # Sessions + file-records go with the FIRST batch only -- they're
            # full upserts and don't need re-sending.
            payload = {
                "user":            user,
                "machine":         machine,
                "sessions":        sessions_list if i == 0 else [],
                "turns":           turns_acc[t_start:t_start + INGEST_BATCH_SIZE],
                "records":         records_acc[r_start:r_start + INGEST_BATCH_SIZE],
                "processed_files": file_records if i == 0 else [],
            }
            resp = api_ingest(cfg, payload)
            batches_sent += 1
            log(
                f"  batch {batches_sent}/{total_batches}: "
                f"{len(payload['turns'])} turns, {len(payload['records'])} records -> "
                f"server got {resp.get('turns_received')}t / {resp.get('messages_received')}m"
            )

        save_state(state)
        result.update({
            "files":    parsed_files,
            "turns":    new_turn_count,
            "records":  new_record_count,
            "sessions": len(sessions_list),
            "batches":  batches_sent,
        })
        ok = True
        return result
    finally:
        elapsed = time.time() - started_at
        if ok:
            log(
                f"PUSH #{push_id} OK in {elapsed:.1f}s - "
                f"{result['turns']} turns across {result['sessions']} sessions, "
                f"{result['batches']} batches",
                section=True,
            )
        else:
            log(
                f"PUSH #{push_id} FAIL after {elapsed:.1f}s "
                f"(see ERROR lines above for cause)",
                level="ERROR",
                section=True,
            )


# ── CLI ─────────────────────────────────────────────────────────────────────

def cmd_push(args):
    cfg, path = load_config(args.config)
    log(f"config loaded from {path}")
    push(cfg, dry_run=args.dry_run)


# ── Daemon singleton lock ──────────────────────────────────────────────────

def _daemon_lock_path() -> Path:
    """Lock file lives next to state.json -- one lock per OS account. Windows
    only allows one Active RDP client per account at a time, so a single
    daemon serving "whoever is currently attached" is the right granularity.
    """
    return _state_path().parent / DAEMON_LOCK_FILENAME


def _pid_is_collector(pid: int) -> bool:
    """Cross-check whether a PID is alive AND belongs to a
    ClaudeUsageCollector.exe process. Tasklist is the Windows-built-in
    way to do this without psutil. We compare the image name to guard
    against PID reuse: PIDs cycle, so a stale lock pointing at a number
    that some unrelated process now uses must NOT block us."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "ClaudeUsageCollector.exe" in (result.stdout or "")
    except Exception:
        # Last-resort POSIX-style probe -- doesn't tell us the image name,
        # but at least tells us a process exists. Errs on the side of
        # "another daemon is alive" when ambiguous; safer than racing.
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _release_daemon_lock(lock_path: Path, owner_pid: int) -> None:
    """Best-effort cleanup. Only removes the lock if it still points at US,
    so a daemon that's been replaced by a different instance doesn't yank
    the active one's lock on exit."""
    try:
        if not lock_path.exists():
            return
        try:
            current = int(lock_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return
        if current == owner_pid:
            lock_path.unlink()
    except Exception:
        pass


def _acquire_daemon_lock() -> bool:
    """Returns True if we successfully claimed the lock (i.e. no other daemon
    is running on this OS account). False if another live daemon already
    has it -- in which case the new daemon should exit cleanly."""
    lock_path = _daemon_lock_path()
    my_pid = os.getpid()

    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            existing_pid = None

        if existing_pid and existing_pid != my_pid and _pid_is_collector(existing_pid):
            log(f"another daemon is already running on this OS account (PID {existing_pid}); exiting")
            return False
        if existing_pid:
            log(f"stale lock from PID {existing_pid} (no longer collector process); taking over")

    try:
        lock_path.write_text(str(my_pid), encoding="utf-8")
    except OSError as e:
        log(f"warning: couldn't write daemon lock at {lock_path}: {e}", level="WARN")
        # Don't block startup on lock-file failure -- single-daemon is a nice
        # property to have but not a correctness requirement.
        return True

    atexit.register(_release_daemon_lock, lock_path, my_pid)
    return True


def _wts_get_current_clientname() -> Optional[str]:
    """Read the CURRENT session's CLIENTNAME from Win32 (not the cached env
    block). The process env is a snapshot from spawn time, but Windows
    updates a session's CLIENTNAME live on RDP reconnect/takeover. The WTS
    API reflects that. Returns None on any failure -- caller treats that
    as "unknown" and stays put.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        wtsapi = ctypes.windll.wtsapi32

        buf = ctypes.c_void_p()
        bytes_returned = ctypes.c_uint32()

        ok = wtsapi.WTSQuerySessionInformationW(
            WTS_CURRENT_SERVER,
            WTS_CURRENT_SESSION,
            WTSClientName,
            ctypes.byref(buf),
            ctypes.byref(bytes_returned),
        )
        if not ok or not buf.value:
            return None
        try:
            value = ctypes.wstring_at(buf.value)
            return (value or "").strip() or None
        finally:
            wtsapi.WTSFreeMemory(buf)
    except Exception:
        return None


def cmd_daemon(args):
    """Run forever, pushing every N seconds.

    This is the default mode for v1.5+ installs -- the installer registers
    an HKLM Run entry that starts 'ClaudeUsageCollector.exe daemon' on
    every login. The process lives until the user logs off.

    On an RDP host with a shared OS account, there's still only one
    daemon: Windows only lets one client be Active on a given account at
    a time (a second client reconnects to the same session). The daemon
    tracks "who's currently attached" by re-reading the live WTS
    CLIENTNAME inside detect_user() on every push, so disconnect +
    reconnect with a different client is handled without a restart.

    Config loading is retried indefinitely with a 10-second backoff. This
    is defense-in-depth against the install-time race that bit v1.6.0,
    where the daemon was launched before config.json had been written by
    the installer. v1.6.1's installer writes config.json before [Run]
    fires, but this retry keeps things working if a future install ever
    hits the same ordering issue, or if config.json is briefly missing
    for any other reason (Sophos quarantine + release, admin editing it
    by hand, etc.).
    """
    log(f"daemon starting (interval: {args.interval}s)")

    # Singleton lock -- refuse to start if another daemon is already running
    # on this OS account. Prevents the "four daemons stacked" scenario where
    # manual restarts + HKLM Run autostarts accumulate.
    if not _acquire_daemon_lock():
        return

    cfg = None
    config_path = None
    waited = 0
    while cfg is None:
        try:
            cfg, config_path = load_config(args.config)
        except FileNotFoundError as e:
            if waited == 0:
                log(f"daemon: config.json not found yet, will retry every 10s")
                log(str(e))
            else:
                log(f"daemon: still waiting for config.json ({waited}s elapsed)")
            try:
                time.sleep(10)
                waited += 10
            except KeyboardInterrupt:
                log("daemon interrupted while waiting for config, exiting")
                return

    log(f"daemon: config loaded from {config_path}")
    user = detect_user()
    log(f"daemon identity: os_username={user['os_username']} source={user.get('identity_source')}")

    # Identity is re-detected inside push() on every iteration via
    # detect_user(), which now reads the live WTS CLIENTNAME. The daemon
    # process can outlive a session disconnect+reconnect and still attribute
    # subsequent turns to whoever's currently attached -- no respawn dance,
    # no env snapshot to go stale.
    while True:
        try:
            push(cfg)
        except KeyboardInterrupt:
            log("daemon interrupted, exiting")
            return
        except Exception as e:
            log(f"daemon push iteration failed: {e}", level="ERROR")
            log(traceback.format_exc(), level="ERROR")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log("daemon interrupted during sleep, exiting")
            return


def cmd_status(args):
    try:
        cfg, path = load_config(args.config)
        print(f"Config:        {path}")
        print(f"Server URL:    {cfg.get('server_url')}")
        print(f"Ingest token:  {'set' if cfg.get('ingest_token') else 'MISSING'}")
        print(f"Projects dirs: {cfg.get('projects_dirs') or [str(p) for p in DEFAULT_PROJECTS_DIRS]}")
    except FileNotFoundError as e:
        print(str(e))

    state = load_state()
    print(f"\nMachine fp:    {state.get('machine_fp')}")
    print(f"State file:    {_state_path()}")
    print(f"Lock file:     {_daemon_lock_path()}")
    print(f"Files tracked: {len(state.get('files', {}))}")
    print(f"Log file:      {_log_path()}")

    # Identity diagnostic. Run this on a misattributing RDP host to see in
    # one shot which signal is winning. If wts_live != env_clientname, you're
    # looking at a session that was taken over by a different RDP client
    # after the daemon spawned -- and v1.7.3 should be picking the wts_live
    # value as the chosen identity.
    machine_fp = state.get("machine_fp") or _make_machine_fp()
    user = detect_user()
    machine = detect_machine(machine_fp)
    print("\nIdentity:")
    print(f"  chosen os_username: {user['os_username']}")
    print(f"  identity_source:    {user.get('identity_source')}")
    print(f"  wts_live:           {_wts_get_current_clientname() or '(none / not RDP)'}")
    print(f"  env_CLIENTNAME:     {os.environ.get('CLIENTNAME', '(unset)')}")
    print(f"  env_SESSIONNAME:    {os.environ.get('SESSIONNAME', '(unset)')}")
    print(f"  is_rdp:             {machine.get('is_rdp')}")
    print(f"  client_machine:     {machine.get('client_machine', '(none)')}")
    print(f"  hostname:           {machine.get('hostname')}")


def cmd_reset(args):
    p = _state_path()
    if p.exists():
        p.unlink()
        print(f"Removed {p}")
    else:
        print("No state to remove.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="collector", description="Claude Code usage collector")
    parser.add_argument("--config", help="Path to config.json (overrides search)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_push = sub.add_parser("push", help="Scan + push new data to the server (one-shot)")
    p_push.add_argument("--dry-run", action="store_true", help="List what would be pushed; don't push")
    p_push.set_defaults(func=cmd_push)

    p_daemon = sub.add_parser("daemon", help="Run forever; push every --interval seconds (default 900)")
    p_daemon.add_argument("--interval", type=int, default=DAEMON_SLEEP_SECONDS,
                          help="seconds between pushes (default: 900)")
    p_daemon.set_defaults(func=cmd_daemon)

    p_status = sub.add_parser("status", help="Print config + state summary")
    p_status.set_defaults(func=cmd_status)

    p_reset = sub.add_parser("reset-state", help="Forget all push history (next push re-processes everything)")
    p_reset.set_defaults(func=cmd_reset)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except CollectorError as e:
        log(f"FATAL: {e}", level="ERROR")
        sys.exit(1)
    except Exception as e:
        log(f"UNHANDLED EXCEPTION: {e}", level="ERROR")
        log(traceback.format_exc(), level="ERROR")
        sys.exit(2)


if __name__ == "__main__":
    main()
