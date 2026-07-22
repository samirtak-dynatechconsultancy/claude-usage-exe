# v1.9.3 — Team Activity actually collects (fixes empty dashboard + no logs)

Two bugs stopped v1.9.x Team Activity from working after install:

## 1. `start_date = today` returned HTTP 400 (no data ever landed)

The daily collector used today's date as the analytics window start. But
claude.ai's admin-analytics endpoint returns per-user activity **since**
`start_date` and **rejects a same-day or future `start_date` with HTTP 400** —
so every run failed and pushed only an `ok=false` status (which then showed up
as a false "cookie expired" flag in the dashboard).

**Fix:** the window now starts on a **past** date. New `analytics_days_back`
config option (default **7**) makes each daily run collect a trailing one-week
window of per-user activity, tagged with the run date. Clamped to `>= 1` so it
can never hit the 400 again. Override per-run with `--start-date YYYY-MM-DD`, or
set `analytics_days_back` in `config.json`.

## 2. No log output from Team Activity runs

The `team-activity` command wrote its output with `print()`, but the collector
`.exe` is built windowed (no console), so that output went nowhere — there was
no way to see whether a run happened or why it failed.

**Fix:** Team Activity now logs through the same logger as everything else, so
runs appear in **`%LOCALAPPDATA%\ClaudeUsageCollector\collector.log`**
(Start Menu → *Open log*) as `team-activity START / … / DONE` sections.

## Also

- The installer now **runs Team Activity once at the end of install** (like the
  Desktop-usage push), so the dashboard shows data immediately instead of
  waiting for the 08:00 task.
- Generated `config.json` includes `"analytics_days_back": 7`.

## Upgrading

Install over the top. If your dashboard was showing a "cookie expired" flag for
an org that actually has a valid cookie, that was this bug — it clears on the
next successful run.
