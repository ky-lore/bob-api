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

Also needs, before first real run:
- A ClickUp API token, Slack bot token, GHL PIT token, and a Google service account
  JSON key — none of these are provisioned yet outside Claude's connector layer.
  See docs/SECRETS_AND_INTEGRATIONS_MAP.md §1.
- The Google service account shared as Viewer on the 3 heartbeat/TV-feed sheets
  (IDs in `app/config.py`) — a service account doesn't inherit human access.

## What's real vs. stubbed

`app/tasks/daily_go_live_audit.py` implements the deterministic parts of the prompt
(heartbeat pull + freshness/CSV-fallback, the LIVE-definition cross-check, package-clock
rules, digest assembly, run/flag persistence) and leaves judgment-heavy correlation logic
as `# TODO(port):` comments — matching a card to a heartbeat row by name, reading GHL sales
notes for the over-promise check, and stage-aware Slack-channel correlation. Don't fill
those in by guessing; they need a decision on the Slack auth question below first, and
ideally a look at the real ClickUp board to design the name-matching approach.

**Open blocker:** `app/integrations/slack.py` — Slack's search API needs a user token
(xoxp), not a bot token (xoxb). The original prompt used org-wide Slack search for
cancel-intent and new-channel detection; a bot token can't replicate that. Needs a call
from Chris/Jaime before the cancellation safety net (ACCURACY RULES §2 in the SKILL.md)
can be ported faithfully — see the full comment in that file.

## Persistence

Postgres via SQLAlchemy (`app/db.py`, `app/models.py`). Three tables:
- `audit_runs` — one row per day, gives the dashboard its history (`AuditRun.run_date`)
- `flags` — one row per finding, tied to a run
- `managed_client_entries` — the exec-editable watchlist/ex-client lists, replacing the
  hardcoded lists in SKILL.md (edit at `/admin/watchlist`)

Schema is created via `Base.metadata.create_all` for now (`init_db()`, runs on startup).
Switch to Alembic migrations once this schema has real production history in it —
don't hand-edit tables at that point.

## Routes

- `GET /dashboard`, `GET /dashboard/{date}` — the priority list. Deliberately plain
  server-rendered HTML right now; this is the piece slated for a real interactive
  rebuild later, not this pass.
- `GET/POST /admin/watchlist`, `POST /admin/watchlist/{id}/deactivate` — no auth wired
  in yet. Don't expose this publicly on Railway before adding some — it edits data that
  feeds a daily report to Chris.
- `POST /tasks/daily-go-live-audit/run` — manual trigger, same code path as the cron.
- `GET /health` — for Railway's health check.

## Tests

`tests/test_daily_go_live_audit.py` covers the pure functions only (heartbeat parsing,
LIVE definition, package-clock rules) — the first test coverage this system has ever had.
Extend it as the TODO-marked correlation logic gets filled in; don't let it stay the only
tests forever (see docs/PROJECT-BRIEF-FOR-NEW-DEV.md §10 item 5).
