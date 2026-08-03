# v1.9.13 — `usage` command no longer crashes on a Cloudflare 403

The Claude Desktop subscription-% (`usage`) task could crash with an unhandled
`HTTP Error 403: Forbidden` traceback. claude.ai intermittently Cloudflare-blocks
standalone requests, and the usage read wasn't wrapped, so a 403 became a scary
UNHANDLED EXCEPTION (and showed up interleaved in the log next to a healthy
Team Activity run).

Fixes:
- **Retry** the Desktop-usage claude.ai calls on transient failures (403/408/429/
  5xx + network errors) with backoff; 400/401/404 stay immediate. Org-probing
  still skips fast (a 403 there just means 'no plan on that org').
- **Skip cleanly** if the read still fails — `usage: skipped - HTTP Error 403`
  instead of a traceback. Desktop subscription % is a best-effort secondary
  metric; a failed read never crashes the run.

Team Activity was unaffected — that's a separate command and was working in the
same log.
