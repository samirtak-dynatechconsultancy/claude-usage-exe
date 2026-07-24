# v1.9.7 — Team Activity shows the collecting device

Each Team Activity push now includes the **device name** and **Windows user** of
the machine that collected it (`source_host` / `os_user`). The dashboard shows
**"Collected from &lt;host&gt; (&lt;user&gt;)"** for the selected organization, so you
can see which admin machine is responsible for each org's data — useful when the
collector is installed across many devices.

No behavior change to collection itself; this is metadata added to the push.
(Requires the matching dashboard update and the `source_host` / `os_user`
columns on `team_activity_daily` and `team_activity_org`.)
