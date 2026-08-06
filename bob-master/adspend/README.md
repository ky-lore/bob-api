# adspend

Ad-spend pull package. Lives in this repo alongside `app/` (the LLM+write
service) and is deliberately self-contained — its own `Settings`, its own
thin `AtlasClient`/`GoogleAdsClient` — so it *could* be lifted into its own
repo/service later without untangling imports. In practice today it's
mounted at `/adspend` on bob-master's own FastAPI app (see `app/main.py`)
and called in-process from `app/tasks/daily_go_live_audit.py` — one
deployment, one base URL, no second Railway service (2026-08-06, reversed
from the original separate-service plan once it was clear that just added
a second base URL to manage for no real benefit at this scale).

Reads the account universe + per-account platform IDs from Atlas
(`integrations.googleMccId`, eventually `metaAdAccountId`), pulls real spend
from the Google Ads / Meta Marketing APIs, and exposes it over a small REST
API — reachable at `<bob-master's URL>/adspend/...` for any consumer,
in-process import for anything living in this repo (see `GoogleAdsClient`
usage in `daily_go_live_audit.py` — no HTTP hop needed for that, it's the
same process).

## Endpoints (under `/adspend` once mounted)

- `GET /adspend/accounts/{customer_id}/spend?date_range=YESTERDAY` — direct
  pull by Google Ads customer ID, no Atlas involved. Useful for smoke testing.
- `GET /adspend/atlas-accounts/{atlas_id}/spend?date_range=YESTERDAY` —
  resolves the customer ID via Atlas first.
- `date_range` — one of `TODAY`, `YESTERDAY`, `LAST_7_DAYS`, `LAST_14_DAYS`,
  `LAST_30_DAYS`, `THIS_MONTH`, `LAST_MONTH` (GAQL's predefined literals).

## Running locally

Runs automatically as part of bob-master:

```
uvicorn app.main:app --reload
```

`adspend`'s own routes are then live at `http://localhost:8000/adspend/...`.
It can still be run standalone (e.g. to iterate on it in isolation) with
`uvicorn adspend.main:app --reload --port 8001`. Either way it reads the same
repo-root `.env` as bob-master (see `.env.example`'s adspend section) — since
it's one deployment now, its env vars just live in bob-master's Railway
service, not a separate one.

## Generating GOOGLE_ADS_REFRESH_TOKEN

One-time manual step (the OAuth client's redirect URI is set to
`https://advancedmarketers.co`, a real site with no callback listener, so
this is a copy-the-code-from-the-URL-bar flow rather than an automated one):

1. Open this URL (fill in your real `GOOGLE_ADS_CLIENT_ID`) in a browser,
   signed in as a Google account with access to the MCC:

   ```
   https://accounts.google.com/o/oauth2/v2/auth?client_id=<CLIENT_ID>&redirect_uri=https://advancedmarketers.co&response_type=code&scope=https://www.googleapis.com/auth/adwords&access_type=offline&prompt=consent
   ```

2. Approve the consent screen. You'll land on advancedmarketers.co with a
   `code=...` query param in the address bar — copy that value (not the whole
   URL).
3. Exchange it for tokens:

   ```
   curl -s https://oauth2.googleapis.com/token \
     -d client_id=<CLIENT_ID> \
     -d client_secret=<CLIENT_SECRET> \
     -d code=<CODE_FROM_STEP_2> \
     -d grant_type=authorization_code \
     -d redirect_uri=https://advancedmarketers.co
   ```

4. The response's `refresh_token` is the value for `GOOGLE_ADS_REFRESH_TOKEN`
   — it does not expire under normal use (only if revoked, unused for 6
   months, or the user's Google security settings change). The `access_token`
   in that same response is short-lived and not needed — `GoogleAdsClient`
   mints its own from the refresh token on demand.

## Deploying

Nothing separate to do — it deploys whenever bob-master does (same
`requirements.txt`, same Procfile, same Railway service). Just make sure
`GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`,
`GOOGLE_ADS_REFRESH_TOKEN`, and `GOOGLE_ADS_LOGIN_CUSTOMER_ID` are set on
bob-master's *existing* Railway service alongside everything else (`ATLAS_API_KEY`
is already there).

## Open question (flagged, not yet resolved)

Atlas's field is named `googleMccId`, but this service treats its value as
the *client's own Google Ads customer ID* (a child account under the shared
MCC in `GOOGLE_ADS_LOGIN_CUSTOMER_ID`), not a second per-client MCC — an MCC
is a manager account, and per-client manager accounts wouldn't make sense
here. Confirm this against real Atlas data before trusting `/atlas-accounts`
in production.
