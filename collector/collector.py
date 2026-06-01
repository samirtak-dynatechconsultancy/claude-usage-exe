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
import glob
import hashlib
import json
import os
import socket
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError


# ── Constants ───────────────────────────────────────────────────────────────

APP_NAME = "ClaudeUsageCollector"
USER_AGENT = "claude-usage-collector/1.2"
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

def _state_path() -> Path:
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
    import getpass
    return {"os_username": (getpass.getuser() or "unknown").strip()}


def detect_machine(machine_fp: str) -> Dict[str, str]:
    return {
        "hostname":   socket.gethostname(),
        "os":         f"{sys.platform} {os.environ.get('OS', '')}".strip(),
        "machine_fp": machine_fp,
    }


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
        log(f"  warning: error reading {filepath}: {e}")

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


def _log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / APP_NAME / "collector.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def log(msg: str) -> None:
    global _LOG_FILE
    if _LOG_FILE is None:
        _LOG_FILE = _log_path()
    # datetime.utcnow() is deprecated in 3.12+; use timezone-aware now() and
    # format manually to keep the same "...Z" suffix the user sees in logs.
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
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
    update local state. Idempotent — already-processed line ranges are
    skipped via the per-file `lines` counter in state.json.
    """
    state = load_state()
    machine_fp = state.get("machine_fp") or _make_machine_fp()
    state["machine_fp"] = machine_fp

    user = detect_user()
    machine = detect_machine(machine_fp)

    # Privacy toggle (config.json: "upload_content": true | false).
    # Default true preserves v1.2.x behavior on upgrade. When false, we send
    # turn-level metadata (tokens, model, project, etc.) but NOT a single
    # word of any prompt or response.
    upload_content = bool(cfg.get("upload_content", True))

    log(
        f"push: user={user['os_username']} machine={machine['hostname']} "
        f"fp={machine_fp[:8]}… content_uploads={'on' if upload_content else 'off'}"
    )

    # ── Discover files ────────────────────────────────────────────────────
    projects_dirs = [Path(p) for p in cfg.get("projects_dirs") or []] or DEFAULT_PROJECTS_DIRS
    jsonl_files: List[str] = []
    for d in projects_dirs:
        if not d.exists():
            continue
        jsonl_files.extend(glob.glob(str(d / "**" / "*.jsonl"), recursive=True))
    jsonl_files.sort()
    log(f"discovered {len(jsonl_files)} jsonl files across {len(projects_dirs)} dirs")

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
    log(f"{len(new_or_changed)} files need processing")

    if dry_run:
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
            log(f"  ERROR processing {fp}: {e}")
            log(traceback.format_exc())
            continue

    log(f"parsed: {parsed_files} files, {new_turn_count} new turns, {new_record_count} new records")

    if not turns_acc and not records_acc and not sessions_acc:
        log("nothing to ingest")
        save_state(state)
        return {"files": parsed_files, "turns": 0, "records": 0, "sessions": 0}

    # ── POST in batches ───────────────────────────────────────────────────
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
        # Sessions + file-records go with the FIRST batch only — they're
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
            f"{len(payload['turns'])} turns, {len(payload['records'])} records → "
            f"server got {resp.get('turns_received')}t / {resp.get('messages_received')}m"
        )

    save_state(state)
    log(
        f"push complete: {parsed_files} files, {new_turn_count} turns across "
        f"{len(sessions_list)} sessions, {batches_sent} batches"
    )
    return {
        "files":    parsed_files,
        "turns":    new_turn_count,
        "records":  new_record_count,
        "sessions": len(sessions_list),
        "batches":  batches_sent,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────

def cmd_push(args):
    cfg, path = load_config(args.config)
    log(f"config loaded from {path}")
    push(cfg, dry_run=args.dry_run)


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
    print(f"Files tracked: {len(state.get('files', {}))}")
    print(f"Log file:      {_log_path()}")


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

    p_push = sub.add_parser("push", help="Scan + push new data to the server")
    p_push.add_argument("--dry-run", action="store_true", help="List what would be pushed; don't push")
    p_push.set_defaults(func=cmd_push)

    p_status = sub.add_parser("status", help="Print config + state summary")
    p_status.set_defaults(func=cmd_status)

    p_reset = sub.add_parser("reset-state", help="Forget all push history (next push re-processes everything)")
    p_reset.set_defaults(func=cmd_reset)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except CollectorError as e:
        log(f"FATAL: {e}")
        sys.exit(1)
    except Exception as e:
        log(f"UNHANDLED EXCEPTION: {e}")
        log(traceback.format_exc())
        sys.exit(2)


if __name__ == "__main__":
    main()
