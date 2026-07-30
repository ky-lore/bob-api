# Live Scheduler Registry — Snapshot

Pulled via the `scheduled-tasks` MCP tool (`list_scheduled_tasks`) on **2026-07-30**. This is the ground-truth list of what's actually firing today — treat it as more current than `BOB-OPERATIONS.md`'s fleet description.

Each task's live `SKILL.md` is reported at a path like `/Users/Bob/Claude/Scheduled/<taskId>/SKILL.md` — **this path is inside Claude's own app storage and could not be mounted/read directly from this session** (it overlaps a protected host location). Where a matching folder exists under `~/Documents/Claude/Scheduled/<name>/`, that archived copy is included under `prompts/local-archive/` and is assumed to be the same or a near-current version — but you should diff it against the live version (ask a Cowork session to `Read` the exact path below) before treating it as authoritative.

| taskId | Cadence | Enabled | Last run | Matching archive file | Notes |
|---|---|---|---|---|---|
| `daily-go-live-audit` | `0 7 * * 1-5` (weekdays 7:01 AM) | true | 2026-07-30 | `prompts/live/daily-go-live-audit.md` (pulled verbatim from the live invocation this session — this one IS current, not archived) | Cloud task. |
| `eod-sales-report` | `0 16 * * *` (daily 4:03 PM) | true | 2026-07-30 | `prompts/local-archive/eod-sales-daily-report.md` (name mismatch — verify same task) | Possible duplicate of `end-of-day-sales-report` below. |
| `daily-account-flag-scan` | Manual only | true | 2026-07-21 | `prompts/local-archive/daily-account-flags.md` (name mismatch — verify same task) | Has the old ClickUp Retention list ID — see mapping doc §4. |
| `weekly-growth-team-qa-scores` | Manual only | true | 2026-07-21 | `prompts/local-archive/weekly-growth-team-qa-scores.md` | Prompt describes a Friday cadence — confirm whether "manual only" is intentional. |
| `weekly-team-call-qa-digest` | Manual only | true | 2026-07-21 | `prompts/local-archive/weekly-team-call-qa-digest.md` | Same note. |
| `monday-growth-qa-email` | Manual only | true | — | `prompts/local-archive/monday-growth-qa-email.md` | Same note. |
| `weekly-sales-team-qa-scores` | Manual only | true | 2026-07-21 | `prompts/local-archive/weekly-sales-team-qa-scores.md` | Same note. |
| `monthly-process-working-session` | Manual only | true | 2026-07-21 | `prompts/local-archive/monthly-process-working-session.md` | Same note. |
| `end-of-day-sales-report` | `30 16 * * 1-5` (weekdays 4:31 PM) | true | 2026-07-30 | (none — likely the duplicate of `eod-sales-report`) | **Resolve this duplication before porting either one.** |
| `calliq-daily-health-check` | `0 8 * * *` (daily 8:09 AM) | true | 2026-07-30 | **none — not documented anywhere in `BOB-OPERATIONS.md` and no archived SKILL.md found.** | Description only: "read the Call IQ pipeline's status heartbeat from Google Drive and report whether last night's 3 AM run succeeded." Pull this prompt fresh via a Cowork session before porting — it's new fleet growth this package couldn't capture. |
| `remote-floor-help-watcher` | `*/10 6-21 * * 1-5` (every 10 min, weekdays 6 AM–10 PM) | true | 2026-07-28 | **none — no archived SKILL.md found.** | Scans `#remote-floor` (`C0BK7R59UMP`) for help keywords, DMs Jaime + Chris. Complements a separately-published Slack Workflow Builder automation (not part of this package — lives entirely in Slack's own workflow config, would need re-export from Slack admin if migrating). Pull this prompt fresh too. |

**Not in this registry at all — separate cloud registry, per `BOB-OPERATIONS.md`/`REBUILD-PACK.md`:**

| Task | Cadence | Notes |
|---|---|---|
| `weekly-churn-early-warning` | Mondays 6:00 AM | Still running under Chris's **personal** Claude account, never migrated to the team account (Phase 4, Option B of `MIGRATION-CHECKLIST.md` was never executed). Captured in `prompts/live/weekly-churn-early-warning.md` from the local archive folder — cross-check against the personal-account registry if you get access to it. |

**Also designed but never scheduled at all:** `ask-bob-team` / `ask-bob-admin` — see `prompts/planned-not-built/`.

## Action item before you write any code

Before treating anything in `prompts/local-archive/` as gospel, open a Cowork session (or ask whoever has one) to run `list_scheduled_tasks` again and `Read` each `path` value fresh, then diff against this package. Three tasks above have zero file-backed prompt (`calliq-daily-health-check`, `remote-floor-help-watcher`, and whichever of the eod-sales duplicates isn't `eod-sales-daily-report`) — those need a fresh pull, they cannot be reconstructed from this filesystem snapshot alone.
