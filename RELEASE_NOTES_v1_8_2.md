# v1.8.2 — Desktop usage uploads automatically after install

## The problem

v1.8.0/v1.8.1 added the Claude **Desktop** subscription‑usage reader (`usage`
subcommand) and a tray "Check usage now" item — but **nothing ran it
automatically**:

- the background **daemon** only pushes Claude **Code** data, never Desktop usage
- the **installer** never registered a task for it
- the **tray** only pushes usage on a manual click

So after a normal install, the dashboard stayed empty for session/weekly usage
unless someone clicked "Check usage now."

## The fix

The installer now, on install (interactive/admin path):

1. **Registers a daily `ClaudeUsageDaily` task** (18:00) via
   `usage --install-task`, running elevated as the install user.
2. **Runs `usage` once immediately** so data appears in the dashboard right away.

Both run in the elevated installer context, so the VSS cookie read works even
while Claude Desktop is open. Uninstall removes the task.

## ⚠ Requires local admin

Reading Claude Desktop's locked cookie needs a Volume Shadow Copy, which needs
elevation. The scheduled task runs at "Highest," which only actually elevates
for users who are **local administrators**.

- **Admin users:** install as admin → usage uploads daily (and immediately). ✅
- **Standard (non‑admin) users:** the task can't elevate, so Desktop usage stays
  empty. Their Claude **Code** collection is unaffected. A non‑admin solution
  would require a SYSTEM‑level component and is out of scope for this release.

Silent/SYSTEM (Intune) installs skip the usage task (SYSTEM can't decrypt the
per‑user cookie).

## Upgrade

Install over the top; the new `[Run]` step registers the task. No config change.
