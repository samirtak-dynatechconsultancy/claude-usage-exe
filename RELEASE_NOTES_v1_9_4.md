# v1.9.4 — Team Activity window defaults to yesterday

v1.9.3 defaulted the daily Team Activity window to the **past 7 days**. This
release changes the default to **yesterday** (`start_date` = yesterday's date),
so each daily run reflects the most recent day rather than a trailing week.

- `analytics_days_back` now defaults to **1** (yesterday). It still must be
  `>= 1` — the claude.ai analytics endpoint rejects a same-day/future
  `start_date` with HTTP 400.
- Want a longer trailing window instead? Set `analytics_days_back` to `7` (or
  any number) in `config.json`, or pass `--start-date YYYY-MM-DD` for a one-off.

No other behavior changes from v1.9.3.
