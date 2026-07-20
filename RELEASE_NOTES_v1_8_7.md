# v1.8.7 — No‑admin cookie read (close/reopen Claude fallback)

## What changed

The Desktop‑usage cookie read now works **without admin** by falling back to
briefly closing Claude. `_copy_cookie_db` tries, in order:

1. **Direct copy** — Claude already closed (no admin, no disruption).
2. **VSS snapshot** — Claude open + admin (no disruption). *Unchanged.*
3. **Close → copy → reopen** — Claude open, no admin: force‑quit Claude so the
   cookie DB unlocks, copy it, then relaunch Claude.

`get_session_cookie` relaunches Claude afterward. A module flag
`ALLOW_CLOSE_CLAUDE = True` disables step 3 if you never want auto‑close.

## Impact

- **Admin machines are unaffected** — they still use VSS silently; Claude is
  never closed.
- **Non‑admin machines** with Claude open now get a reading, at the cost of a
  brief Claude restart. ⚠️ Schedule this **once a day at a quiet hour**, not
  every few minutes — otherwise it restarts the user's Claude repeatedly. An
  in‑progress prompt draft can be lost on force‑quit.

## Not included (deliberately)

The scheduled task is still registered at "Highest" for the install user and
skipped in silent installs. Running this **as the logged‑on user for a silent
Intune fleet** (which is what activates fleet‑wide Claude restarting) is a
separate change — pending an explicit decision.
