# v1.9.9 — Team Activity: seat tier + full member roster

The collector now also fetches the org's **full member roster** (claude.ai
`/members`) each run and pushes it, so the dashboard can show:

- a **Seat tier** column (e.g. Team Standard, Team Tier 1, Unassigned), and
- **every seat holder** — including members who have **never been active**
  (they show with a blank "last active" and zero activity), not just users who
  were active in the selected range.

No new configuration. Requires the matching dashboard update and a one-time
`ALTER TABLE team_activity_org ADD COLUMN roster jsonb` (migration 0013).
