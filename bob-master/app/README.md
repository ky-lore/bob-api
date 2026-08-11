# app/ — Python/FastAPI port, first task

First real port of a task out of `tasks/daily-go-live-audit/SKILL.md`. Local-first,
targeting Railway (Postgres add-on + this app as a single web process).

## Setup

```
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in real values — see docs/SECRETS_AND_INTEGRATIONS_MAP.md
./.venv/bin/uvicorn app.main:app --reload
```

Needs Python 3.10+ (Railway: set via `runtime.txt` or its Python version picker).
`app/models.py` deliberately uses `Optional[X]` instead of `X | None` so it also runs
on older local Pythons if someone's machine lags behind — no functional reason to need
3.10+ beyond that, so don't "clean this up" back to `|` without checking that constraint
still applies.

Also needs, before first real run: a ClickUp API token, Slack bot token, GHL PIT token,
a Google service account JSON key (base64-encoded — see `app/integrations/google_drive.py`
docstring), and an Anthropic API key (dashboard narrative synthesis). See
`docs/SECRETS_AND_INTEGRATIONS_MAP.md` §1 and `.env.example`.

## What's real vs. stubbed

Built against real sandboxed data (Go-Live board, Web Build Pipeline, Retention Pipeline,
heartbeat sheets) via the `/admin/*-sample` debug endpoints, not guessed at — several bugs
only showed up once real data was pulled (timezone assumptions, column-header mismatches,
numeric-only account names, template/demo cards mixed into real boards). See git history
for the specifics; each fix cites what real data surfaced it.

**Implemented:** heartbeat pull + freshness (same-Pacific-calendar-day, not a rolling
window) + CSV-fallback; cross-platform LIVE-definition check; fuzzy card↔account matching
(`app/tasks/matching.py`, calibrated confidence tiers, human-editable alias table) wired to
real package-clock evaluation (`app/tasks/clickup_correlation.py`); Retention pipeline
cancel-intent cross-check (ACCURACY RULES §2, `app/tasks/retention_check.py`); GHL
Closed-Won sweep; digest assembly + Slack send; and a dashboard whose stat tiles are
computed deterministically while the per-account "what's blocking" narrative is
synthesized by one batched Claude call per run (`app/integrations/anthropic_client.py`,
`app/tasks/dashboard_summary.py`) — the one part of the original system that was itself
LLM-narrated, not a fixed template.

**Deliberately not built yet** (see `docs/TASK-INVENTORY.md`/chat history for why each
was deprioritized in favor of macro coverage over micro accuracy):
- Web Build Pipeline stale-build sweep — logic not written; also, the sandbox list's
  `date_created` was reset by ClickUp's duplication feature, so day-counts there won't be
  trustworthy until tested against production data anyway.
- Stage-aware per-status checks (PREPARATION/ONBOARDING/DEVELOPMENT/PRE-LIVE/etc.)
- GHL over-promise check (reading sales notes for new Closed Won deals)
- ClickUp writes of any kind (tag auto-apply, status moves, card creation) — everything
  today is read-only
- GHL "Package Type" field lookup (package-ID priority #2) — no bridge from account name
  to GHL contact ID exists

**Open blocker:** `app/integrations/slack.py` — Slack's search API needs a user token
(xoxp), not a bot token (xoxb). Needed for org-wide cancel-intent search and reading
channel history for stage-aware checks. Needs a decision from Chris/Jaime.

## Persistence

Postgres via SQLAlchemy (`app/db.py`, `app/models.py`). Since schema is still evolving
faster than Alembic is worth setting up, `init_db()` does more than `create_all`:
- `create_all` — creates missing tables/enum types (never alters existing ones)
- `_sync_postgres_enum_values()` — adds new values to an existing Postgres enum type
  (`create_all` won't; bit us once already — see git history)
- `_sync_postgres_missing_columns()` — adds new columns to an existing table (same gap,
  different flavor). Any column added this way must be nullable — `ADD COLUMN` fails on
  a non-empty Postgres table otherwise.

Switch to real Alembic migrations once this schema has real production history in it —
don't hand-edit tables at that point, and don't keep extending the sync functions above
past what's convenient for early development.

Tables: `audit_runs` (one row per day — `dashboard_json` holds the stat tiles + narrative
rows, `digest_text` holds the Slack DM body), `flags` (one row per finding), and
`managed_client_entries` (exec-editable watchlist/ex-client/alias lists, `/admin/watchlist`).

## Routes

- `GET /dashboard`, `GET /dashboard/{date}` — stat tiles + "what's blocking" narrative
  table (from `AuditRun.dashboard_json`), plus the raw flag list underneath as a
  transparent detail view.
- `GET/POST /admin/watchlist`, `POST /admin/watchlist/{id}/deactivate` — no auth wired
  in yet. Don't expose this publicly on Railway before adding some — it edits data that
  feeds a daily report to Chris.
- `POST /tasks/daily-go-live-audit/run` — manual trigger, same code path as the cron. Runs on
  a background thread and returns `{job_id, job_status: "running"}` immediately rather than
  blocking (a full run over the real account universe can outrun any client/proxy timeout) —
  poll `GET /tasks/daily-go-live-audit/run/{job_id}` for the result. See `app/tasks/job_tracker.py`.
- `GET /admin/clickup/{go-live,web-build,retention}-sample`, `GET /admin/sheets/tv-board-feed`,
  `GET /admin/heartbeat/headers` — debug endpoints, real board/sheet data. Use these
  before writing any new correlation logic instead of guessing at structure.
- `POST /admin/slack/join-public-channels` — idempotent, has the bot self-join every
  public channel it's not already in.
- `GET /health` — for Railway's health check.

## Tests

50+ tests, mostly built directly against real data pulled via the debug endpoints above
(real ClickUp card titles, real sheet headers) rather than synthetic examples — this is
what caught the LSA-column bug, the timezone bug, and the ambiguous-match threshold
miscalibration. `tests/test_run_daily_go_live_audit_e2e.py` exercises the full
`run_daily_go_live_audit()` pipeline against a real (temp file) SQLite DB with the four
external clients faked out — proves the wiring executes correctly at runtime, not just
compiles.
