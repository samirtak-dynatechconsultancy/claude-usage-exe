# v1.9.0 — Team Activity: daily per-user claude.ai admin analytics

## What's new

A new **`team-activity`** command collects claude.ai's per-user **admin
analytics** (chats, messages, projects, code sessions, days active, estimated
spend) for one or more organizations and pushes it to the dashboard **daily**.

Why it lives in the collector: the dashboard server (Vercel) can't call
claude.ai — Cloudflare blocks its datacenter IP. Running the request from an
admin's own machine, with a real browser Cookie, gets through. Only the parsed
per-user rows leave the machine.

### Configure it — `config.json`

Add an `analytics_orgs` array, one entry per organization you administer:

```json
"analytics_orgs": [
  {
    "org":      "28df4ba0-1849-47c8-a2ac-f42d7796edc0",
    "org_name": "Dynatech",
    "cookie":   "sessionKey=sk-ant-sid...; cf_clearance=...; __cf_bm=..."
  }
]
```

- **`org`** — the organization UUID (from the `/organizations/<UUID>/…` URL).
- **`cookie`** — the **full Cookie header** copied from a logged-in claude.ai
  request (DevTools → Network → any request → Request Headers → Cookie). It must
  contain `sessionKey=…`; including `cf_clearance`/`__cf_bm` helps clear
  Cloudflare.
- **`org_name`** — a label for the dashboard's organization dropdown.

Add as many orgs (each with its own cookie) as you like. Leave the array empty
to disable collection.

### How it runs

- The installer registers a daily **`ClaudeTeamActivityDaily`** scheduled task
  (default **08:00**, catch-up enabled). It no-ops until you add `analytics_orgs`.
- Change the schedule:
  `ClaudeUsageCollector.exe team-activity --install-task --at 07:30`
  (or `--every N --unit days|weeks`).
- Run it on demand: **Start Menu → Run team activity now**, or
  `ClaudeUsageCollector.exe team-activity`.
- Pagination climbs `page=1,2,3…` (page_size 50) until claude.ai returns an
  empty `members` list, so every user is captured no matter the team size.

### On the dashboard

The **Team Activity** tab is now DB-backed: pick an **organization** and a
**date** to view that day's per-user activity. If an org's cookie has expired
(or Cloudflare blocked it), that org is flagged with a ⚠ and a banner telling
you to refresh its cookie in `config.json`.

## Also in this release (rolled up from the unreleased 1.8.11)

`usage` now **skips cleanly** on machines without Claude Desktop instead of
logging a scary traceback — `FileNotFoundError`, `PermissionError` (user
declined the restart), `ValueError` (no sessionKey / v20 / bad key), and
`OSError` (DPAPI decrypt failed) are all treated as a one-line
`usage: skipped - <reason>` and exit 0. Claude **Code** collection is
unaffected.

## Upgrading

Install over the top — the installer stops the running processes, refreshes the
binaries, keeps your `config.json`, and registers the new daily task. Then add
`analytics_orgs` to `config.json`. **Dashboard side:** run migration
`0012_team_activity_daily.sql` and redeploy.
