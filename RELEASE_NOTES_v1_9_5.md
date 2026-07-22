# v1.9.5 — Team Activity: true per-day data + backfill + reset

(v1.9.4 shipped the smaller "default to yesterday" change; this release adds the full per-day model below.)

## Per-day windows (not a trailing 7-day total)

Each Team Activity run now collects **one calendar day** of per-user activity
using the window `[day, day+1)` — so the numbers are that day alone, not a
running total since some start date. The claude.ai analytics endpoint does
support an `end_date`, which makes clean single-day snapshots possible.

- **Default daily run** collects **yesterday** (the endpoint 400s on today, and
  only a completed day has settled data). Configurable with
  `analytics_days_back` (1 = yesterday) or `--date YYYY-MM-DD`.
- Each day is stored under its own `snapshot_date`, so the dashboard's date
  dropdown shows one entry per day.

## Backfill history

```
ClaudeUsageCollector.exe team-activity --backfill 30
```

Collects the **last 30 days, one day at a time**, up to yesterday. Pass a number
to change the span (`--backfill 60`). Great for populating the dashboard with a
month of history in one go.

## Reset

```
ClaudeUsageCollector.exe team-activity --reset            # wipe all, then...
ClaudeUsageCollector.exe team-activity --reset --backfill 30
```

`--reset` asks the dashboard to delete stored team-activity data (all orgs)
before repopulating — a clean rebuild. (Requires the matching dashboard update
that adds the reset endpoint.)

## Notes

- Removed the old `--start-date` flag; use `--date` (single day) or `--backfill`.
- `analytics_days_back` now means "which single day to collect," default **1**
  (yesterday), not a trailing-window length.
