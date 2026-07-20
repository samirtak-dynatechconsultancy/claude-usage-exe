# v1.8.6 — No console popup on usage collection

Previously, each time the usage task ran, a command‑prompt window flashed on
screen for a few seconds (the collector exe is a console app, and the task ran
it directly in the interactive session).

## Fix

The usage task now launches through a tiny hidden VBS launcher
(`run_usage.vbs`) via `wscript`, which starts the collector with a **hidden
window** — so nothing pops up on each collection. The install‑time "push once
now" step goes through the same launcher.

- New file shipped to the install dir: `run_usage.vbs`.
- `usage --install-task` registers the task action as
  `wscript run_usage.vbs` (falls back to the exe directly when the VBS isn't
  present, e.g. running from source).
- Elevation is preserved: the task still runs at Highest, so wscript — and the
  exe it spawns — stay elevated for the VSS cookie read.

No behaviour change otherwise. Reinstall (or upgrade) to pick it up; existing
schedules are re‑registered by the installer.
