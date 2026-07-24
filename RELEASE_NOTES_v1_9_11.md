# v1.9.11 — Fix Team Activity double-counting (single-day windows)

## The bug

Team Activity numbers in the dashboard were roughly **2× the real values**
(e.g. 176 chats shown vs 89 actual), and "days active" was inflated.

claude.ai's analytics endpoint treats `end_date` as **inclusive**. The daily
collector used a `[day, day+1]` window, which therefore covered **two** days.
Because consecutive days overlapped by one, summing them across a range
double-counted almost everything.

## The fix

Each day is now collected with `start_date == end_date == day` — exactly one
day. Verified against claude.ai: summing the corrected single-day values equals
the endpoint's range total.

## Action required

The stored history was collected with the old (overlapping) window, so it must
be rebuilt once:

```
"C:\Program Files\ClaudeUsageCollector\ClaudeUsageCollector.exe" team-activity --reset --backfill 45
```

(run elevated; safe to re-run — one row per user per day). After that the
dashboard totals match the claude.ai admin export.
