# v1.9.12 — Team Activity: resilient to sleep/offline (retry + catch-up)

If the machine slept or lost network mid-run, a day could fail and be lost
(e.g. `getaddrinfo failed` / connection reset right after wake). Two fixes:

## Retry with backoff
Every claude.ai fetch, roster fetch, and push now retries transient failures
(DNS-not-ready-on-wake, connection reset, Cloudflare 403/503, non-JSON
challenge) up to 5 times with 5/10/20/40 s backoff. HTTP 400/401 stay
immediate. This bridges the short window after a wake when the network isn't up
yet.

## Daily catch-up
A plain daily run now tops up the **last 3 days** (idempotent upsert) instead of
only yesterday, so a short off/offline stretch self-heals rather than leaving a
gap. Configurable via `analytics_catchup_days` in config.json (default 3; set to
1 for strict yesterday-only). Existing configs without the key get 3
automatically.

To patch an older gap immediately: `team-activity --backfill 7`.
