# v1.8.8 — Silent/Intune Desktop usage for standard users (with a consent popup)

Non-admin machines can now report Claude Desktop usage via a silent Intune
install — **and the user is always asked before their Claude is touched.**

## How it works

- The usage task is registered to run as **BUILTIN\Users** (the logged-on user),
  and it's now created in **silent installs too** (`--fleet`, no `skipifsilent`).
- When the task runs and the cookie DB is locked (Claude open, no admin), the
  collector shows a **Yes/No popup**:

  > "To record your Claude usage, the collector needs to briefly close and
  >  reopen Claude Desktop. Save anything you're typing first. Close and reopen
  >  Claude now?"

  - **Yes** → it closes Claude, reads, and relaunches it.
  - **No / closed** → it skips this run quietly and tries again next time.
- Admin machines are unaffected (VSS, no popup, no close). Machines with Claude
  already closed just read it directly (no popup).

So nothing closes a user's Claude without their explicit consent in the moment.

## Deploy notes

- **Schedule once a day.** The interactive installer's schedule page still
  applies; the silent default is once daily. Don't set minute/hour cadences for
  a fleet — that would prompt people repeatedly.
- The consent screen (attended installs) now discloses this; the runtime popup
  is the notice for silent installs.
- Intune: package the exe as before (`/VERYSILENT /SUPPRESSMSGBOXES`); the usage
  task now registers under SYSTEM and runs as the logged-on user.

## Still off the table

Closing Claude **without** asking. The popup is mandatory in the no-admin path.
