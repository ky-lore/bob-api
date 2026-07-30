# Task Inventory — filename ↔ taskId ↔ status

Built while reorganizing `prompts/` into `tasks/` (2026-07-30). This is the map for
what landed where and why, plus the gaps the other docs don't fully surface. Cross-reference
with `docs/SCHEDULED_TASKS_REGISTRY_SNAPSHOT.md` (scheduler ground truth) and
`docs/SECRETS_AND_INTEGRATIONS_MAP.md` (per-task integrations/IDs).

## Confirmed in the live/manual scheduler registry

| Folder (`tasks/<name>/`) | taskId | Cadence | Prompt source | Status |
|---|---|---|---|---|
| `daily-go-live-audit` | `daily-go-live-audit` | Weekdays 7:01 AM (cloud) | pulled live, current | clean |
| `eod-sales-report` | `eod-sales-report` | Daily 4:03 PM | archived as `eod-sales-daily-report.md` — **name doesn't match taskId, unverified same task** | needs verification |
| `daily-account-flag-scan` | `daily-account-flag-scan` | Manual only | archived as `daily-account-flags.md` — **name mismatch, unverified** | needs verification + has old ClickUp retention list ID (see integrations map §4) |
| `weekly-growth-team-qa-scores` | same | Manual only (prompt implies Friday) | archived, name matches | confirm cadence; has old ClickUp retention list ID |
| `weekly-team-call-qa-digest` | same | Manual only | archived, name matches | confirm cadence |
| `monday-growth-qa-email` | same | Manual only | archived, name matches | confirm cadence; has old ClickUp retention list ID |
| `weekly-sales-team-qa-scores` | same | Manual only | archived, name matches | confirm cadence; publishes via an inline plaintext password — see secrets map §1 |
| `monthly-process-working-session` | same | Manual only | archived, name matches | confirm cadence |
| `weekly-churn-early-warning` | same | Mondays 6 AM, **cloud, under Chris's personal account** | `SKILL.md` = the live pull (current, canonical); `SKILL.superseded-local-archive-copy.md` kept alongside — diffed near-identical, only frontmatter differs | needs Phase-4 migration to team account |

## No recoverable prompt at all (placeholder folders created)

| Folder | taskId | Cadence | Why empty |
|---|---|---|---|
| `end-of-day-sales-report` | `end-of-day-sales-report` | Weekdays 4:31 PM | Likely duplicate of `eod-sales-report` — **resolve which is canonical before pulling a fresh copy of either** |
| `calliq-daily-health-check` | `calliq-daily-health-check` | Daily 8:09 AM | Not documented anywhere outside the registry snapshot; never had an archived file |
| `remote-floor-help-watcher` | `remote-floor-help-watcher` | Every 10 min, weekdays 6 AM–10 PM | Same — never had an archived file |

Each has a `NEEDS-FRESH-PULL.md` explaining how to backfill it.

## Not in any fleet listing anywhere in this package (`tasks/_unregistered/`)

These 7 have full prompt text, real credentials, and real system IDs referenced in
`docs/SECRETS_AND_INTEGRATIONS_MAP.md` — but **none of them appear in the "current scheduled
fleet" table in the project brief, the registry snapshot, or `BOB-OPERATIONS.md`.** That's not
a naming mismatch like the ones above — it's a total absence from every fleet listing in this
package. Before doing anything else with these, find out from Chris/Jaime whether they're:
retired, running under a mechanism this package doesn't cover, or just missing from the
registry pull.

- `bluepoint-daily-digest` — client digest, depends on Chris's personal MacBook script (see brief §2)
- `pinpon-daily-digest` — same dependency
- `piggies-daily-digest` — same dependency
- `daily-zoom-commitments` — only task noted as using the Zoom MCP connector instead of raw API
- `weekly-ad-winners` — likely feeds the `winning-creatives` dashboard, but that link is inferred, not documented
- `weekly-hoo-brief`
- `monthly-hoo-performance-brief`

## Planned, never scheduled (`planned/`)

- `ask-bob-team`
- `ask-bob-admin`

Two-tier Slack access pattern (tool-scoping, not prompt-trust) — worth preserving the *design*
even though there's no running task to migrate.

## Dashboards

Exported HTML nested one-per-folder as `dashboards/<artifact-id>/index.html`. Note:
**`golive-pipeline-dashboard` has no exported HTML in this package** even though the project
brief documents it as one of the six Cowork artifacts — the 6 files here map to
`ad-optimization-live`, `churn-early-warning`, `negative-keyword-review`,
`retention-command-center`, `retention-mission-control`, `winning-creatives`. Confirm whether
the go-live dashboard needs a fresh export.
