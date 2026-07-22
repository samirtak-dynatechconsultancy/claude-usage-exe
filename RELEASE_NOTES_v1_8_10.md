# v1.8.10 — Clean upgrade so no stale cmd windows linger

## Background

The exe is built `--windowed` (no console), and v1.8.9 put `CREATE_NO_WINDOW`
on every child process it spawns (tasklist, powershell, taskkill, esentutl,
schtasks). So current code has no console-window source.

The cmd windows some machines still saw came from a **stale usage task left by
an older version** (v1.8.7/1.8.8 spawned those children *without* the flag).
An in-place upgrade replaced the exe but the old task kept firing until it
next re-registered.

## Fix

The installer now, before copying files, **deletes the old `ClaudeUsageDaily`
task** (and already force-kills the collector/tray processes). The install then
re-registers a clean task that runs the windowed exe with all child windows
suppressed. So upgrades can't leave a flashing task behind.

## If you still see a cmd window: clean reinstall

1. Uninstall from Add/Remove Programs.
2. Reboot (clears any lingering daemon/tray/usage process).
3. Install v1.8.10.
4. Set the schedule to **Days** (once a day), not minutes/hours.

No behaviour change to collection itself.
