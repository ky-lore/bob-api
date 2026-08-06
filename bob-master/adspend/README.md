# adspend

Standalone ad-spend pull service. Lives in this repo alongside `app/` (the
LLM+write service) for now, but is deliberately self-contained — its own
`Settings`, its own thin `AtlasClient`/`GoogleAdsClient` — so it can be lifted
into its own repo later without untangling imports.

Reads the account universe + per-account platform IDs from Atlas
(`integrations.googleMccId`, eventually `metaAdAccountId`), pulls real spend
from the Google Ads / Meta Marketing APIs, and exposes it over a small REST
API for any consumer (bob-master's dashboard, or anything else) to read.

## Endpoints

- `GET /accounts/{customer_id}/spend?date_range=YESTERDAY` — direct pull by
  Google Ads customer ID, no Atlas involved. Useful for smoke testing.
- `GET /atlas-accounts/{atlas_id}/spend?date_range=YESTERDAY` — resolves the
  customer ID via Atlas first.
- `date_range` — one of `TODAY`, `YESTERDAY`, `LAST_7_DAYS`, `LAST_14_DAYS`,
  `LAST_30_DAYS`, `THIS_MONTH`, `LAST_MONTH` (GAQL's predefined literals).

## Running locally

```
uvicorn adspend.main:app --reload --port 8001
```

Reads the same repo-root `.env` as bob-master (each service just needs its
own vars present — see `.env.example`'s adspend section).

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

## Deploying as its own Railway service

Same repo, same `requirements.txt` (adspend's dependencies — fastapi,
uvicorn, httpx, pydantic-settings — are already in it). Add a **second**
Railway service pointing at this repo, Root Directory left at the repo root,
with a custom Start Command:

```
uvicorn adspend.main:app --host 0.0.0.0 --port $PORT
```

Set this service's own env vars (`GOOGLE_ADS_*`, `ATLAS_API_KEY`) in Railway
directly — a second service does not inherit the first service's env vars.

## Open question (flagged, not yet resolved)

Atlas's field is named `googleMccId`, but this service treats its value as
the *client's own Google Ads customer ID* (a child account under the shared
MCC in `GOOGLE_ADS_LOGIN_CUSTOMER_ID`), not a second per-client MCC — an MCC
is a manager account, and per-client manager accounts wouldn't make sense
here. Confirm this against real Atlas data before trusting `/atlas-accounts`
in production.
