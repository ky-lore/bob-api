# Secrets, Integrations & ID Reference Map

Compiled from every prompt in `/prompts`. **No secret values appear in this file or anywhere in this package** — only variable names, storage locations, and non-secret system IDs (list IDs, channel IDs, etc. — these identify records, they don't grant access on their own).

---

## 1. Secrets inventory (values live only on the Mac mini today — rotate these when you set up real env vars)

| Variable / credential | Currently stored at | Used by | Notes |
|---|---|---|---|
| `GHL_API_KEY` (format `pit-...`, a GoHighLevel Private Integration Token) | `~/Documents/Claude/Scheduled/weekly-team-call-qa-digest/ghl-credentials.txt` | Every task that calls GHL directly: `daily-go-live-audit`, `eod-sales-daily-report`, `weekly-growth-team-qa-scores`, `weekly-sales-team-qa-scores`, `daily-account-flags`, `monday-growth-qa-email` (reads tracker only) | One token, one scope, shared by every task — no per-task scoping today. |
| `GHL_LOCATION_ID` | same file | same tasks | The main agency GHL location. Note: **client sub-account digests use different location IDs** (see §4) — not this one. |
| Zoom Server-to-Server OAuth: `ACCOUNT_ID`, `CLIENT_ID`, `CLIENT_SECRET` | `~/Documents/Claude/Scheduled/weekly-team-call-qa-digest/zoom-credentials.txt` | `eod-sales-daily-report`, `weekly-growth-team-qa-scores`, `weekly-sales-team-qa-scores`, `daily-account-flags`, `weekly-hoo-brief`, `monthly-hoo-performance-brief`, `weekly-team-call-qa-digest`, `daily-zoom-commitments` (via Zoom MCP connector, not raw API) | Client-credentials grant (no user OAuth flow); 1-hour tokens, refresh on 401. |
| Internal HTML upload host login | Embedded **inline in the prompt text** of `weekly-sales-team-qa-scores` (not a credentials file) | Publishing the weekly Sales QA HTML report to `https://advancedmarketers.co/internalupload/` | **Risk finding:** this is a plaintext credential living inside a prompt, not a secrets file — the worst-scoped secret in the whole system. Rotate and move to proper storage before this becomes a real deploy. |
| Stray duplicate credential files | `.../weekly-team-call-qa-digest/eod_0720/`, `eod_0721/`, `eod_0721_v2/`, `eod_0723/` | none (leftover copies from past runs) | Delete / never commit. These exist because past runs copied the whole folder forward as a scratch workspace. |

**Not a stored secret — OAuth via MCP connector (no local file):** ClickUp, Slack, Google Drive, Google Calendar, Supermetrics, Zoom-as-connector. Auth lives in Claude's own connector store, tied to the `bob@advancedmarketers.co` login. When you port this to FastAPI, each of these needs its own credential (API token or OAuth app) provisioned independently — there is currently no exported client ID/secret for any of them, because Claude's connector layer has been doing that job.

**Recommended for the FastAPI/Railway version:** one `.env` per environment (local dev vs Railway), loaded via `pydantic-settings` or `python-dotenv`; Railway's own environment variable store for the deployed instance; never commit `.env`. Suggested variable names: `GHL_API_KEY`, `GHL_LOCATION_ID`, `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`, `SLACK_BOT_TOKEN`, `CLICKUP_API_TOKEN`, `SUPERMETRICS_API_KEY` (or client OAuth pair), `GOOGLE_SERVICE_ACCOUNT_JSON` (Drive/Sheets/Calendar), `INTERNAL_UPLOAD_HOST_PASSWORD`.

---

## 2. Integrations used, by task

| Integration | How it's called today | Tasks using it |
|---|---|---|
| **GoHighLevel (GHL)** | Direct REST, `https://services.leadconnectorhq.com/...`, `curl` + PIT token. Required headers on every call: `Authorization: Bearer $GHL_API_KEY`, `Version: 2021-07-28`, `Accept: application/json`, `User-Agent: AM-QA/1.0` (omitting `User-Agent` gets a WAF 403). | `daily-go-live-audit`, `eod-sales-daily-report`, `weekly-growth-team-qa-scores`, `weekly-sales-team-qa-scores`, `daily-account-flags`, `piggies-daily-digest`, `pinpon-daily-digest`, `bluepoint-daily-digest`, `monday-growth-qa-email` (indirect, via tracker file) |
| **Zoom** | Server-to-server OAuth (`POST https://zoom.us/oauth/token?grant_type=account_credentials`), then REST (`/v2/users/{email}/recordings`, `/v2/meetings/{uuid}/recordings`). Some tasks use the raw API directly (via bash), others use the Zoom MCP connector's higher-level tools (`search_meetings`, `get_recording_resource`) — **inconsistent today, worth standardizing on one approach.** | Raw API: `eod-sales-daily-report`, `weekly-growth-team-qa-scores`, `weekly-sales-team-qa-scores`, `daily-account-flags`, `weekly-hoo-brief`, `monthly-hoo-performance-brief`, `weekly-team-call-qa-digest`. Connector-based: `daily-zoom-commitments`. |
| **ClickUp** | MCP connector tool surface (create/update/move/tag/comment tasks, filter, time-in-status). Workspace `10552018`. | Nearly every task — either to file a summary task in "Chris - Action Items" (`901417226802`) or to read/update the Go-Live / Retention / Web Build pipelines. |
| **Slack** | MCP connector tool surface (read channel/thread, search, send message, reactions, canvas). Workspace `advancedmarketers.slack.com`. | `daily-go-live-audit`, `daily-account-flags`, `weekly-growth-team-qa-scores` (canvas update), `monthly-process-working-session` (spot-check), plus the not-yet-built `ask-bob-team`/`ask-bob-admin`. |
| **Google Drive / Sheets** | MCP connector (`read_file_content`, `download_file_content` as a CSV-export fallback when the former returns empty). | `daily-go-live-audit` (heartbeat sheets), `weekly-ad-winners` (GHL lead sheet). |
| **Google Calendar** | MCP connector (`list_events`, event creation). | `daily-zoom-commitments`. |
| **Supermetrics** | MCP connector (`data_query` + `get_async_query_results`, `accounts_discovery`, `field_discovery`). ds_ids in use: `AW` (Google Ads), `FA` (Meta/Facebook), `TIK` (TikTok). | `weekly-ad-winners`, `weekly-churn-early-warning`, `piggies-daily-digest`, `pinpon-daily-digest`, `bluepoint-daily-digest`, and (as a cross-check) `daily-go-live-audit`. **Actively being replaced** — see `docs/migration-history/FREE-DATA-STACK-PILOT.md`. |
| **Claude in Chrome (browser automation)** | Used once, to log into and upload a file to an internal password-protected host. | `weekly-sales-team-qa-scores` (publishing step). |
| **Local filesystem dependency on Chris's personal MacBook** | Three daily digest tasks depend on an **unidentified local script on `/Users/christianpaniagua/...`** (not the Mac mini) that pulls GHL call recordings + transcripts into a folder every morning before Bob's digest runs. This script is NOT part of the Claude/Cowork system and its implementation is unknown from this side. | `piggies-daily-digest`, `pinpon-daily-digest`, `bluepoint-daily-digest` — **all three have a hard dependency on Chris's personal machine being on and that script having run.** Flag this to Chris/Jaime as a single point of failure worth understanding before migration. |

---

## 3. System IDs referenced across prompts (safe to keep in code/config — these are record identifiers, not credentials)

### ClickUp
- Workspace: `10552018`
- Go-Live Pipeline: list `901417990784` ("Go-Live Pipeline (last 90 days)")
- Web Build Pipeline: list `901413623955`
- "Chris - Action Items" (space "chris p"): list `901417226802` — where most weekly/monthly summary tasks get filed
- Retention pipeline — **CURRENT** name/ID per `BOB-OPERATIONS.md`: `901417897821` ("RETENTION & CANCELLATION CLIENTS", statuses: new requests / happy holding area / save attempt (48 hrs) / free month active / saved+ / churned x)
- Retention pipeline — **OLD, deprecated** per ops doc: `901417799940-44` (Change Management space) — **⚠️ drift found:** `weekly-growth-team-qa-scores`, `daily-account-flags`, and `monday-growth-qa-email` prompts *still reference the old ID `901417799940`*, even though `BOB-OPERATIONS.md` and `REBUILD-PACK.md` say this was corrected. Fix this when you port these three.
- Slack canvas "Growth Team QA Scores": `F0BEZ05TGP9` (channel `#growth-team`)

### Slack
- Christian Paniagua: `U01GYV63X9D`
- Jaime Falcon: `U0B0XG32YLV`
- `#account-flags-daily`: `C0BFFCMD24S`
- `#teamleads`: `C0A9EF303S5`
- `#remote-floor`: `C0BK7R59UMP`
- `#ask-bob` / `#ask-bob-admin`: not yet created (see planned prompts)

### GoHighLevel (main agency location)
- ADV Master Pipeline: `1rySFshGqxtuO5hF2z2f`
  - stage "Closed - Digital Diagnosis": `23efcfe7-9b9a-4e6f-9b00-b768263b68ff`
  - stage "Closed Won": `9fff7088-7251-470b-99b5-dc2374630cde`
- RAW - HOUSTON pipeline: `EN99INlGiYdView6PvaR`
  - stage "SOLD -Digital Diagnosis": `fc2fc43e-6469-48bc-b39c-fee65101405b`
- Contact custom fields: `vcSOMOQUD0U6LzoLg2oy` (Slack channel ID), `XMXL75J1u9Tu55IDHG4D` (Package Type), `PrWBcqWMlGqWBVwfFY3a` (sales-notes doc link)
- Rep user IDs (growth): Tim `yni4V3GaXAUx7dWihm2d`, Mak `TY3fz3pyEdLSziGdLoqO`, Julie `DFMIyKzjgUcM8AKMWnzQ`, Johnny `WWfkycgYv41qsQUviK0X`, Simon `0KtKhe81CmuuMYZMmm83` (Lindsey/Donovan resolved dynamically each run via `/users/?locationId=` — never hardcoded, since they were unassigned at prompt-authoring time)
- Rep user IDs (sales): Jaden `oaxfQ14pFppgbEY1QtUs`, Kyle Kellner `zFFksf6pFR54PxcMNgUI`, Anthony `1HdaBF0747C6J5O5PBYB`, Angel `SPtnJNgiol0fWq547GwQ`, Paul (VP) `VcAw8YaXQU5UKKrQqmc7`, Z Stewart (departed) `Ecln3wutPNN3Hl127zUf`, Patrick Schwerdtfeger (departed) `WJEqIqSvBSW0RGGYGj6s`
- Shared phone lines: Sales One `n6MVmciOLkoraZskdp3b`, Sales Two `qxeGrSMchI1QkyR8zqJq`

### GoHighLevel (per-client sub-account locations — separate from the main agency location above)
- BluePoint Pools: `V9nRy2IgzPaW84jFenrA`
- Pinpon Junk Removal: `eqjg1NwImDPZ4bEJPwvK`
- Piggies Air Conditioning: location ID not hardcoded in its prompt — resolved at runtime

### Google Drive file IDs
- Google Ads heartbeat sheet: `1PjcXsvoFmSP8nVM_C_Qa7MeEVF6yUEAv6xH_LLKV-KQ` (tab "Heartbeat")
- Meta heartbeat sheet: `1rOsGhG4_vyTRBhR62LD2f6Xhqmg636WqYdvGLx5ZamA` (tab "Heartbeat-Meta")
- TV board feed sheet: `1mtDzvPkxcYXKjlRcWTC37yoTx0jaiaxbfI9H8_nUYdA` (has an "Ignore" tab = ex-clients)
- GHL lead sheet "GHL - Lead Update Meta Report": `17qrXY-1HKwj7A8elnJJX93amezXYbKY4jmuNIKIrzAY`

### Supermetrics
- `ds_id "AW"` = Google Ads, `ds_id "FA"` = Meta, `ds_id "TIK"` = TikTok
- Full client account-ID rosters (60+ Google Ads IDs, ~33 Meta IDs) are in `prompts/live/weekly-churn-early-warning.md` — not re-typed here since that file is the single source of truth for the roster.
- Individual client accounts used by the per-client digests: BluePoint Google Ads `1031051775`; Pinpon Google Ads `9475349656`; Piggies Google Ads resolved dynamically via `accounts_discovery`; Piggies Meta `act_383628763095207` (worth double-checking — this ID matches "Advanced Marketers" itself in the Meta heartbeat sheet, not Piggies; possible mislabel to verify with Chris); TikTok `7631268331820761089`.

### Ex-clients (exclude from all flags/scans — per `daily-go-live-audit`)
Joa Brothers, Paradise Concrete, SM Brothers, Prestige Builders & Design (collections case, not a payment flag), Elite Electric NTX — plus anything on the TV feed sheet's "Ignore" tab.

---

## 4. Known drift found while compiling this map

1. **Old vs new Retention pipeline ID** — three current prompts (`weekly-growth-team-qa-scores`, `daily-account-flags`, `monday-growth-qa-email`) still point at the deprecated ClickUp list `901417799940` instead of the current `901417897821`. `REBUILD-PACK.md` claims this was fixed in `daily-account-flags` specifically — it was not, in the copy currently on disk. Fix all three during the port.
2. **`eod-sales-report` vs `end-of-day-sales-report`** in the live scheduler registry look like duplicates of `eod-sales-daily-report` — resolve which is canonical before porting.
3. **Internal upload host password is inline in a prompt**, not in a credentials file (§1) — needs to move to real secret storage.
4. **Three client digests depend on an unknown script on Chris's personal MacBook**, outside the Bob/Cowork system entirely (§2) — needs investigation before those three tasks can be ported reliably.
