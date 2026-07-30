# Bob → Python/FastAPI Migration Package

Compiled 2026-07-30 for import into VS Code / Claude Code. This package is the full inventory of what "Bob" (Advanced Marketers' Claude/Cowork automation account) currently does, so it can be rebuilt as a real Python service instead of a set of Claude agent prompts.

**Reorganized 2026-07-30** from the original flat `prompts/` package into a per-task layout
(see `docs/TASK-INVENTORY.md` for the full filename ↔ taskId ↔ status map, including gaps
the original package didn't surface). Git has not been initialized on purpose — that's on
the incoming dev.

## Read in this order

1. **`docs/PROJECT-BRIEF-FOR-NEW-DEV.md`** — architecture overview: what Bob is today, the two "Scheduled" concepts, MCP connectors, dashboards, current fleet, known drift. Note: its suggested repo shape (`/tasks/<task-id>/SKILL.md`) is the layout already applied here.
2. **`docs/TASK-INVENTORY.md`** — the map of every prompt file to its scheduler taskId and status (confirmed / name-mismatch / no-file / **not in any fleet listing at all**). Read this before trusting any single task folder.
3. **`docs/SECRETS_AND_INTEGRATIONS_MAP.md`** — every secret (name + location, never values), every integration and how it's called, every system ID referenced in the prompts. Start here for anything involving auth.
4. **`docs/SCHEDULED_TASKS_REGISTRY_SNAPSHOT.md`** — the live scheduler's task list as of this pull, cross-referenced against the archived prompt files, with known gaps flagged.
5. **`tasks/`** — one folder per task, each with a `SKILL.md` (the prompt — the closest thing this system has to source code / business logic):
   - Folders named after the confirmed scheduler `taskId` hold the authoritative prompt.
   - `tasks/<name>/NEEDS-FRESH-PULL.md` marks taskIds with zero recoverable prompt text in this package.
   - `tasks/_unregistered/` holds prompts that exist but appear in **no** fleet listing anywhere in this package — verify these are still real before porting them.
6. **`planned/`** — designed but never scheduled (`ask-bob-team`, `ask-bob-admin`) — a two-tier Slack access pattern worth preserving if you rebuild a chat-triggered request flow.
7. **`docs/migration-history/`** — prior migration docs (MacBook → Mac mini, July 2026) kept for context on why things are structured the way they are. Not required reading, but explains a lot of the naming and the Supermetrics-replacement plan already in flight.
8. **`dashboards/`** — exported HTML for the Cowork Artifact dashboards, one folder per artifact ID (`dashboards/<artifact-id>/index.html`). These are self-contained pages that call MCP tools live; treat them as a UI reference/spec, not code to run as-is. Note: `golive-pipeline-dashboard` has no exported HTML here — see `docs/TASK-INVENTORY.md`.

## What each prompt actually encodes

Every `SKILL.md` under `tasks/` is natural-language business logic: which API to call, which fields to pull, how to score/classify things, what counts as a flag, and where to write output. There is no separate "business logic layer" to look for — the prompt text **is** the spec. When porting a task to Python, treat its prompt as the requirements doc and the referenced IDs/headers (see the integrations map) as the implementation detail.

## Suggested Python/FastAPI shape (starting point, not gospel)

Given local-first now, Railway later:

```
bob/
  app/
    main.py                 # FastAPI app; health check + manual-trigger endpoints
    config.py                # pydantic-settings, reads .env locally / Railway env vars in prod
    scheduler.py              # APScheduler (or Celery beat) — cron per task, mirrors the cadences
                               # in docs/SCHEDULED_TASKS_REGISTRY_SNAPSHOT.md
    integrations/
      ghl.py                  # thin REST client: base URL, required headers, pagination helper
      zoom.py                  # server-to-server OAuth + recordings/transcripts
      clickup.py                # task CRUD, matches the MCP tool surface you're replacing
      slack.py                  # slack_sdk wrapper: read channel/thread, send, react
      google_drive.py            # google-api-python-client, sheet read + CSV export fallback
      supermetrics.py             # or swap for official Google Ads API + Meta Graph API per
                                    # docs/migration-history/FREE-DATA-STACK-PILOT.md
    tasks/
      daily_go_live_audit.py       # one module per folder in /tasks, matching taskId
      weekly_churn_early_warning.py
      ...
    dashboards/                     # FastAPI routes serving JSON the dashboards/*.html can
                                      # be adapted to call, or re-platform dashboards as
                                      # server-rendered Jinja2 templates
  tests/
  .env.example                       # variable names only, see SECRETS_AND_INTEGRATIONS_MAP.md
  requirements.txt / pyproject.toml
  Procfile or railway.json             # Railway deploy config when you're ready
```

Notes for whoever (Claude Code) implements this:
- Each `tasks/*.py` should read like the prompt it replaces — pull the same fields, apply the same scoring/flag rules, write to the same destinations (ClickUp task, Slack message, HTML file). The prompts already encode the acceptance criteria; resist rewriting the business rules "cleaner" without confirming with Chris/Jaime, since several (scoring weights, package clocks, flag thresholds) are explicit owner decisions.
- The credential-duplication and copy-forked-script patterns described in the project brief (§5, §10) are exactly what a real `integrations/` module fixes — parameterize by date/window instead of forking files.
- Fix the three known drift items in `SECRETS_AND_INTEGRATIONS_MAP.md` §4 as part of the port, not after.
- Get `calliq-daily-health-check` and `remote-floor-help-watcher` prompts fresh (see the registry snapshot) before assuming their behavior from the descriptions alone.
- Decide on `eod-sales-report` vs `end-of-day-sales-report` before writing the sales-report module twice.

## What's intentionally NOT in this package

- Any actual secret values (API keys, tokens, passwords) — see the mapping doc for where they currently live on the Mac mini; you'll need to re-collect them into your own `.env`/Railway config directly from the source, not from this zip.
- Call transcripts, recordings, and historical HTML report snapshots — large, sensitive, and not needed to rebuild the logic.
- The six Cowork Artifact dashboards' live data bindings (they call Claude's `window.cowork.callMcpTool()`, which won't exist outside Cowork) — the HTML is included as a design reference only.
