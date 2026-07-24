# v1.9.8 — Configure the Team Activity collection time in the installer

The **Team Activity** wizard page now has a **"Daily collection time (HH:MM)"**
field, so you choose when the daily `ClaudeTeamActivityDaily` task runs during
setup instead of it being fixed at 08:00.

- Default is still **08:00**; enter any 24-hour `HH:MM` (validated).
- Team Activity is always a once-a-day task (it collects the previous complete
  day), so only the time is configurable — not the interval.
- **Silent/Intune installs**: pass `/TEAMTIME=HH:MM` (defaults to 08:00).

You can still change it after install without reinstalling:

```
"C:\Program Files\ClaudeUsageCollector\ClaudeUsageCollector.exe" team-activity --install-task --at 20:00
```

(run elevated). The time lives in the Windows scheduled task, not in
`config.json`.
