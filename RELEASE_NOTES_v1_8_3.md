# v1.8.3 — Configurable usage collection interval

The installer now lets you choose **how often** Claude Desktop usage is
collected, instead of a fixed daily 18:00.

## What's new

Two new wizard pages during setup:

1. **Unit** — Minutes, Hours, Days, or Weeks.
2. **Interval** — "run every how many", plus a time-of-day (used for
   Days/Weeks only).

Examples you can pick:
- every **30 minutes**
- every **2 hours**
- every **1 day** at 18:00
- every **2 weeks** at 09:00

It repeats on that interval indefinitely. Minute/hour cadences repeat from
install time and don't wake the PC; day/week cadences fire at the chosen time,
wake from sleep, and catch up a missed run.

## Under the hood

- `collector usage --install-task` now accepts `--every N --unit
  minutes|hours|days|weeks --at HH:MM`.
- The installer builds those args from the wizard selections
  (`{code:GetUsageParams}`).
- `usage --install-task 09:00` still works (back-compat: daily at that time).

## Unchanged caveat

Still requires the user to be a **local admin** (VSS needs elevation). Standard
users and silent/SYSTEM (Intune) installs don't get Desktop usage — see
RELEASE_NOTES_v1_8_2.
