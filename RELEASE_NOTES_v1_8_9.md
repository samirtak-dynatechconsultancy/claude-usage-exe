# v1.8.9 — No more flashing PowerShell/console windows

## The bug

A console window (sometimes an elevated "admin PowerShell") would flash during
install and on some usage runs.

Cause: the collector shells out to `powershell` (Register-ScheduledTask,
Get-CimInstance), `taskkill`, `esentutl`, and `schtasks` via `subprocess.run`
without suppressing the child's console window. Even though the collector runs
hidden, those child processes popped their own window — and during install the
PowerShell was elevated, hence "admin PowerShell."

## The fix

All background subprocess calls now pass `CREATE_NO_WINDOW` — no window appears.
(Launching Claude itself is unchanged; that's the app, not a console.)

Cosmetic only; no behaviour change. Reinstall/upgrade to pick it up.
