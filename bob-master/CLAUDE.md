# bob-master — project guide

FastAPI service for Advanced Marketers (marketing agency). Replaces a set of
Claude/Cowork agent prompts (see `README.md` / `docs/` for that original
migration-package context — mostly historical now) with real scheduled jobs,
integrations, and dashboards. Hosted on Railway, Postgres-backed.

Production URL: **https://bob-api-production-9ba0.up.railway.app**
Repo: `github.com/ky-lore/bob-api`, branch `main`, auto-deploys on push.

## Stack

- FastAPI + Jinja2 (server-rendered HTML dashboards, not a JS SPA) + SQLAlchemy/Postgres.
- `app/main.py` mounts routers: `dashboard`, `admin`, `atlas_report`, `atlas_campaign_push`, `zoom_call_sync`. Also mounts a separate `adspend` sub-app on the same deployment.
- Integrations (`app/integrations/`): Atlas (internal CRM, source of truth for accounts/stage), ClickUp, Slack, Zoom, Google Ads, Meta Ads, Anthropic (Claude, for narrative synthesis).
- `.env` holds real API keys locally (`ANTHROPIC_API_KEY`, `ATLAS_API_KEY`, `CLICKUP_API_TOKEN`, `GOOGLE_ADS_*`, `META_*`, `SLACK_*`, `ZOOM_*`, `GHL_*`, `DATABASE_URL`). **`DATABASE_URL` in local `.env` is Railway's unresolved `${{Postgres.DATABASE_URL}}` template** — it does NOT work for a local script to hit the real prod DB directly. There is no Railway CLI or Node/npm installed in this dev environment as of 2026-09-21 — don't assume you can `railway run`/`railway status`.
- Tests: `python -m pytest -q` from repo root. **306 tests as of 2026-09-21**, all passing. Run the full suite before every commit — this repo has caught real regressions this way (see gotchas below).

## Git / push conventions actually used in this repo

- Standing pre-authorization (Bob, 2026-09-28: "you're good to push bob"): push to this repo's `main` without asking each time. Still surface what's being pushed and why; still never push while a long Pulse run is in flight (see below); still check `git status`/`git diff` first so an unrelated in-progress file never rides along in the same commit.
- Railway kills any in-flight background job on redeploy (job_tracker is in-memory only, doesn't survive a restart) — **don't push while a long Pulse run is running**, and re-trigger after confirming the deploy is healthy.
- After every push: poll `GET /reports/atlas-account-status/pulse` (or `/latest`) until it 200s, to confirm the redeploy actually landed and didn't crash-loop. Don't spam progress messages to the user every poll tick — poll quietly in the background and report once, on completion.
- Never add secrets to `.env` when the user has explicitly said not to (see Admin override password and logo.dev token below) — this repo has two deliberate, discussed exceptions to the normal "secrets go in .env" default.

## Account Pulse — weekly exec-facing account-health dashboard

This is the main feature built out across recent sessions. Read this whole
section before touching anything under `atlas_report`.

### What it is

A weekly, LLM-synthesized health report over the **entire** Atlas account
universe (~148 accounts as of the last full run), rendered as a server-side
HTML dashboard execs actually look at. Distinct from the older
`daily_go_live_audit` task (which is gated/incremental and only about
go-live readiness) — Pulse is unbounded and covers every active account,
live or not.

### Why it's async/persisted, not a simple synchronous GET

A full unbounded pull of all ~148 accounts (ClickUp + Slack + Zoom context
gather, Google/Meta ad spend, then an LLM blend call per batch) takes
**~10-11 minutes**. That's past Railway's ~300s gateway timeout on a
synchronous request. So:

- `POST /reports/atlas-account-status/run` kicks off a background job, returns `{job_id, job_status: "running"}` immediately.
- `GET /reports/atlas-account-status/run/{job_id}` polls job status (`running` / `done` / `error`).
- The job also writes a row to `AtlasReportRun` (Postgres) as its **durable** result — `job_tracker` itself is in-memory and does NOT survive a Railway redeploy, confirmed the hard way. `GET .../latest` and `GET .../pulse` always read from `AtlasReportRun`, never from `job_tracker`, so the dashboard survives restarts even if the in-memory job status is lost.
- `GET /reports/atlas-account-status/pulse` (and `/pulse/{run_id}` for a specific historical run) renders the actual HTML dashboard from the latest (or specified) `AtlasReportRun` row.
- `GET /reports/atlas-account-status` is the original synchronous endpoint — kept for Atlas's own cron, NOT used by the dashboard.

**To manually kick off a fresh run** (e.g. before a demo, to repopulate with current data):
```
curl -X POST https://bob-api-production-9ba0.up.railway.app/reports/atlas-account-status/run
# -> {"job_id": "...", "job_status": "running"}
# poll (quietly, ~15-20s interval, don't message the user every tick):
curl https://bob-api-production-9ba0.up.railway.app/reports/atlas-account-status/run/{job_id}
# wait for job_status != "running", then check /pulse renders.
```
Last full run: **run_id 6, 2026-09-28T16:37:23Z, 149 accounts** — first run with the standup-weighting prompt change (`40cc7ff`) live; 30/30 narrative batches ok, 85 accounts carried evidence quotes.

### Key files

| File | Responsibility |
|---|---|
| `app/tasks/atlas_report.py` | `build_atlas_report()` — the core builder. Loops every active Atlas account, gathers ClickUp/Slack/Zoom context + Google/Meta ad spend, calls the LLM blend, applies health overrides. `run_and_store_atlas_report(db, ...)` persists a new `AtlasReportRun`. |
| `app/routers/atlas_report.py` | All HTTP routes (see table above), the admin-override endpoints, pipeline/live stage classification, `_display_ready()` (template pre-computation), logo.dev icon URL constants, `_pulse_context()` (the full Jinja context builder). |
| `app/integrations/anthropic_client.py` | `synthesize_account_reports()` → batches of 5 accounts per Claude tool-use call, returns `{health, status, recent_work, evidence}` per account. `_verify_evidence_quotes()` guards against hallucinated citations. |
| `app/tasks/account_context_gather.py` | `gather_atlas_context()` — pulls ClickUp/Slack/Zoom raw context for one account. `business_hours_cutoff()` — weekday-aware hour-stepping cutoff, used ONLY for the display-only "recent ClickUp activity" dropdown, never narrows what the LLM sees. |
| `app/templates/account_pulse.html` | The actual dashboard page — single Jinja template, inline `<style>`, minimal vanilla JS (view toggle, override form, theme toggle). No JS framework. |
| `app/models.py` | `AtlasReportRun`, `AccountHealthOverride`. |

### Data model

**`AtlasReportRun`**: `id`, `run_at` (indexed), `limit_used` (nullable — debug cap), `report_json` (Text — the full `{count, accounts, narrative_batches}` payload as JSON). One row per run, append-only.

**`AccountHealthOverride`**: `atlas_id` (unique, indexed), `company_name`, `health`, `reason` (nullable), `set_by` (nullable), `created_at`, `updated_at`. One row per account, no history — a new override just replaces the row. Applied twice: once baked into `report_json` at generation time (`build_atlas_report`), and again live at render time (`_apply_live_overrides` in the router) so a correction shows up immediately without waiting ~11 minutes for a new full run.

### The per-account record shape

Produced by `build_atlas_report`, one dict per account:
```
{atlas_id, company_name, stage, day, is_live,
 google_ads, google_ads_error, meta_ads, meta_ads_error, ad_spend,
 zoom_call_count, recent_clickup_activity,
 health, status, recent_work, evidence,
 health_overridden, health_override_reason, llm_health (if overridden)}
```
`google_ads`/`meta_ads` are `None` if the account has no ID on file OR the pull failed (soft-failed — never drops the record). `evidence` is `[{source: "slack"|"zoom", quote: "..."}]`, 0-3 entries, always present (defaults to `[]`).

### The LLM blend call (`anthropic_client.py`)

One Claude tool-use call per batch of `_BATCH_SIZE = 5` accounts (`_run_in_batches`). `max_tokens = min(_MAX_TOKENS_CAP=4096, max(_MIN_TOKENS=512, _TOKENS_PER_ACCOUNT=150 * multiplier * batch_len))`. The multiplier has been bumped each time a new output field was added (2 → 3 when `health` was added → currently **4** for `evidence`) specifically to avoid re-triggering a real `max_tokens`-truncation production incident this module's docstring warns about. If you add another output field, bump the multiplier again.

Output fields per account: `health` (enum, defaults to `on_track` if missing/invalid), `status`, `recent_work`, `evidence`. `_coerce_list()` defensively unwraps Claude's occasional double-JSON-encoding of the `reports` array (a real bug hit 2026-08-06) — reuse this pattern if you add more list-shaped output fields.

**Evidence verification** (`_verify_evidence_quotes`, added 2026-09-21): every returned evidence quote is checked — whitespace/case-insensitive substring match — against that account's own gathered `context` strings (passed into the batch call). A quote that doesn't actually appear is silently dropped. Rationale: a hallucinated-but-plausible quote is worse than no evidence at all, since it carries an implicit claim of verbatim accuracy. This is pure string matching, no extra API call.

### Pipeline vs Live classification

Membership in the "Not live yet" (pipeline) section is **stage-based**, not `is_live`-based:
- `_PIPELINE_STAGES = {"onboarding", "development"}` → pipeline section.
- `_EXCLUDED_STAGES = {"closed"}` → excluded entirely from Pulse (neither section).
- Everything else (including `at_risk` stage) → Live section, by elimination.

This was a real bug fix (2026-09-21): the original logic used `is_live` (stage == "live"), which incorrectly bucketed Closed AND At-Risk-stage accounts into "Not live yet" alongside genuine pre-launch accounts. Confirmed against real data at the time: 148 accounts total, 3 onboarding + 14 development + 13 closed + 118 live — meaning 13 of 30 accounts in the old "Not live yet" bucket were wrongly-included Closed accounts. If you touch this logic, re-verify against real `/latest` data before shipping.

`_needs_extra_focus(a)` (drives the card's visual glow/highlight): `_is_pipeline_stage(a) and a.get("health") in ("at_risk", "needs_attention")` — pre-launch accounts that are ALSO flagged get extra visual weight, since execs focus on those more than flagged-but-already-live accounts.

### Admin health override (manual correction)

Rudimentary, deliberately-not-real auth: `_ADMIN_OVERRIDE_PASSWORD = "wasp"`, hardcoded identically in `app/routers/atlas_report.py` and as `ADMIN_PASSWORD` in `account_pulse.html`'s inline `<script>`. **This is intentional** — explicit user instruction: "just save the password in the frontend lol - this is completely internal facing" / "forget .env - can we just store it static on-page?" Do not "fix" this into an env var or real auth layer without being asked; a `Settings.admin_override_password` field was added then explicitly removed earlier in this project's history for exactly this reason.

Override endpoints: `POST .../overrides` (upsert), `DELETE .../overrides/{atlas_id}` (clear), both gated by `X-Admin-Password` header via `_require_admin_password`.

### Logo/icon CDN (logo.dev)

`_LOGO_DEV_TOKEN = "pk_cffuq5CoSbaMfA7fgrXOPg"` — a **publishable** key (`pk_` prefix, logo.dev's own convention, same idea as Stripe's `pk_`/`sk_` split). Hardcoded directly in `app/routers/atlas_report.py`, same reasoning as the admin password: it's visible in every rendered `<img src>` regardless of where it lives in source, so an env var buys no real protection, just indirection. Do not move this to `.env`.

`_LOGO_DEV_DOMAINS = {google: google.com, meta: meta.com, clickup: clickup.com, slack: slack.com, zoom: zoom.us}` → `_LOGO_URLS` computed once at import time (not per-request). URL pattern: `https://img.logo.dev/{domain}?token={token}&size=28&format=png`. **`format=png` is required** — logo.dev's default response is an opaque JPEG with a real solid background baked in.

**Dark-mode transparency gotcha (real, confirmed via Pillow pixel inspection):** Google/ClickUp/Slack/Zoom all come back genuinely transparent RGBA even with `format=png`. **Meta's does not** — it comes back RGB with a solid white background baked in, no alpha channel at all, confirmed by checking the corner pixel (`(255,255,255,255)` vs `(*, *, *, 0)` for the others). Fix: `.platform-icon-opaque` (white backing plate, `background:#fff; border-radius:3px; padding:1.5px`) is applied **only** to Meta's `<img>` (`{{ ' platform-icon-opaque' if p.key == 'meta' }}`). Every other icon renders bare so it sits cleanly on both light and dark card surfaces. If logo.dev ever fixes Meta's transparency, this special-case can be dropped — re-verify with the Pillow pixel check before removing it, don't assume.

If you add a new platform's icon, **check its actual alpha channel before assuming it's transparent** — don't default to giving it the white plate "to be safe," and don't assume it's clean either.

### Recent ClickUp activity dropdown (display-only, separate from the LLM's context window)

Per-card `<details class="recent-activity">` (native, no custom JS) showing ClickUp comments from the last `_RECENT_CLICKUP_ACTIVITY_HOURS = 48` **weekday** hours. `business_hours_cutoff()` in `account_context_gather.py` steps hour-by-hour, skipping weekend time entirely — computed from a Monday, 48 weekday-hours reaches back ~4 real calendar days (through the skipped weekend).

**Critical invariant, explicitly requested:** this cutoff is display-only and must NEVER narrow what the LLM actually sees. The LLM blend always gets the full 7-day (`context_window_days=7`) context window regardless of this dropdown. `gather_atlas_context(..., recent_activity_hours=...)` is an optional, additive parameter — the daily audit's call site doesn't pass it and is unaffected.

### Communication context dropdown (evidence quotes)

Sibling `<details class="recent-activity">` (same visual pattern, reused CSS), placed **underneath** the ClickUp activity dropdown in DOM order. Summary line shows both Slack and Zoom logo.dev icons + "Communication context — N quote(s) cited". Body is a `<ul class="evidence-list">` of `{source, quote}` pairs, each with its own source icon (`logo_urls[e.source]`) — no text label, icon only. Hidden entirely (`{% if a.evidence %}`) when empty — most quiet/on-track accounts have no evidence, and that's the correct/expected state, not a failure.

### Known, already-fixed gotchas (don't reintroduce)

1. **CSS `filter` containing-block bug**: any non-`none` `filter` on an ancestor (even `blur(0)` at rest) creates a new containing block for `position:fixed` descendants per spec. The `.pulse-switch` bottom nav bar was originally nested inside `.page` (which always carries a `filter` for the blur-transition effect), silently breaking its true full-viewport fixed positioning — it was fixed relative to `.page`'s box instead. Fixed by moving `<nav class="pulse-switch">` to be a body-level **sibling** of `.page`, not a descendant. If you add any new `position:fixed` element inside `.page`, watch for this.
2. **`business_hours_cutoff`-adjacent test flakiness**: a test using a naive "N real days ago" timestamp can non-deterministically pass/fail depending on what day of the week the suite runs, because the weekday-only cutoff's real reach varies with today's weekday. Use a wide enough margin (the existing tests use 5 real days for a 48-weekday-hour cutoff) and comment why.
3. **Jinja autoescaping**: `{{ logo_urls[key] }}` inside a `src="..."` attribute gets `&` escaped to `&amp;` in the rendered HTML — don't assert on a literal un-escaped URL string in tests; assert on a substring that doesn't include the query string, or unescape before comparing.
4. **`"override-flag" in text` type assertions**: the literal class name also appears inside the `<style>` block's own CSS rules — always assert on `'class="override-flag"'` (with the `class=` prefix) to actually test the rendered element, not just presence of the string anywhere on the page.

### Building an Artifact preview for this page

The Artifact tool's CSP blocks all external network requests (images included). Every real preview of this page (brand logos, logo.dev icons) needs a throwaway copy with images inlined as base64 `data:` URIs — fetch each unique image URL once (there are only ~7: 2 brand logos + 5 logo.dev icons), base64-encode, string-replace into the HTML. The real template keeps referencing the live external URLs; only the preview copy is embedded. Use `curl` to fetch (plain `urllib`/`requests` hit real local SSL cert-verification failures in this dev environment — `curl` doesn't).

To generate a sample page locally without hitting real production data: spin up a `TestClient` against `router_mod.router` with a real (non-`:memory:` — use a tmp file, in-memory SQLite loses state across connections) SQLite DB, insert a fabricated `AtlasReportRun` row with representative `accounts` JSON, hit `/reports/atlas-account-status/pulse`, then run it through the image-inlining step above.

## Current state (as of 2026-09-22)

- Evidence-citation feature (Slack/Zoom quotes) shipped and live: commits `4637e07` (backend + inline display) and `79840b5` (Communication-context dropdown + Meta-only icon backing plate fix).
- Last full Pulse run: run_id 3, 2026-09-21T23:10:42Z, 148 accounts — currently live on `/pulse`.
- Full test suite: 306 passed, 0 failing, as of the last push.
- No open/pending Pulse work is currently tracked — the last few requests (evidence feature → dropdown reorg → dark-mode icon fix) have all been shipped and verified.
