# Claude Code Usage Collector

End-user agent that scans Claude Code's local JSONL transcripts and pushes
parsed metadata (always) and conversation content (opt-in) to the team
dashboard server.

For end users, this is installed silently via the Windows installer in
[installer/](../installer/) and runs as a Scheduled Task every 15 minutes.
You almost certainly want to start there.

## Running manually (for development)

```powershell
# stdlib only - no pip install needed
cd collector
copy config.example.json config.json
notepad config.json    # fill in server_url + ingest_token

python collector.py status            # show config + state
python collector.py push --dry-run    # list files that would push
python collector.py push              # actually push
python collector.py reset-state       # forget push history
```

## Config file lookup order

1. `--config <path>` CLI flag
2. `$env:COLLECTOR_CONFIG`
3. Directory containing the running script / .exe
4. `%APPDATA%\ClaudeUsageCollector\config.json`
5. Current working directory

## Config fields

| Field | Type | Default | Purpose |
|---|---|---|---|
| `server_url` | string | required | Vercel deployment, e.g. `https://your-app.vercel.app` |
| `ingest_token` | string | required | Shared team secret, matches `INGEST_TOKEN` on Vercel |
| `upload_content` | bool | `true` | `true` sends full conversation text (lets the dashboard "view conversation" modal show what was discussed). `false` sends only token counts, models, project names, and timestamps - **no prompt or response text leaves the machine**. |
| `projects_dirs` | list / null | `null` | Override the default JSONL scan dirs. `null` = scan `~/.claude/projects` and the Xcode coding-assistant dir. |

To switch from full content to metadata-only later: edit `config.json`, set `"upload_content": false`, wait up to 15 minutes for the next scheduled run (or trigger `ClaudeUsageCollector.exe push` manually). Already-uploaded content stays on the server until an admin deletes it.

## Local state

- **State:** `%LOCALAPPDATA%\ClaudeUsageCollector\state.json`
  Tracks per-file mtime and line count so resumed pushes only send the new tail
  of growing files.
- **Logs:** `%LOCALAPPDATA%\ClaudeUsageCollector\collector.log`
  Rotated when the file exceeds 5 MB (keeps last 2 MB).

Delete `state.json` to force a full re-push. The server deduplicates assistant
messages on `message_uuid`, so this is safe.

## What gets sent

Always (regardless of `upload_content`):

- **user**: OS username (e.g. `samir.tak`)
- **machine**: hostname + stable fingerprint (sha256 of hostname + MAC)
- **sessions**: session UUID, project name, git branch, first/last timestamps
- **turns**: model, token counts (input/output/cache_read/cache_write), tool name, working directory, timestamp - one row per assistant API response

Only when `upload_content` is `true`:

- **records**: raw user / assistant message objects, parsed server-side into the `messages` table with structured columns for text, tool calls, and tool results. Enables the dashboard's "view conversation" drill-down and Postgres full-text search.

Everything goes via a single `POST /api/ingest` per batch (50 turns / 50 records per request). No Supabase Storage uploads since v1.2.
