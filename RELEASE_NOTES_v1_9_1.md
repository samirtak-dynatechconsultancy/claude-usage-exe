# v1.9.1 — Enter Team Activity org + cookie pairs during setup

v1.9.0 added the daily Team Activity collector but only accepted its org +
cookie pairs via hand-editing `config.json` after install. This release adds a
**setup wizard page** so you can enter them during installation.

## What's new

A new **"Team Activity (optional)"** page in the installer wizard. Enter one
organization per line, in this format:

```
ORG_UUID | Label | Cookie
```

- **ORG_UUID** — the organization UUID (from the `/organizations/<UUID>/…` URL).
- **Label** — any name shown in the dashboard's organization dropdown.
- **Cookie** — the **full Cookie header** from a logged-in claude.ai request
  (DevTools → Network → any request → Request Headers → Cookie; must contain
  `sessionKey=…`).

Add as many organizations as you like — one per line, each with its own cookie.
The wizard writes them into `config.json`'s `analytics_orgs`, JSON-escaping
cookies that contain quotes or braces (e.g. the `g_state` cookie). Leave the box
empty to skip; you can still add or edit orgs later via **Start Menu → Edit
config**. Malformed lines are caught with a clear message before the install
proceeds.

Everything else is unchanged from v1.9.0 (daily `ClaudeTeamActivityDaily` task,
pagination until `members: []`, dashboard org dropdown + date filter +
expired-cookie flags).

## Upgrading

Install over the top. Your existing `config.json` values pre-fill where
applicable; enter your org + cookie pairs on the new page. **Dashboard side** (if
you haven't already): run migration `0012_team_activity_daily.sql` and redeploy.
