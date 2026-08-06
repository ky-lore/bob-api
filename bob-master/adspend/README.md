# adspend

Ad-spend pull package. Lives in this repo alongside `app/` (the LLM+write
service) and is deliberately self-contained — its own `Settings`, its own
thin `AtlasClient`/`GoogleAdsClient`/`MetaAdsClient` — so it *could* be lifted
into its own repo/service later without untangling imports. In practice
today it's mounted at `/adspend` on bob-master's own FastAPI app (see
`app/main.py`) and called in-process from `app/tasks/daily_go_live_audit.py`
— one deployment, one base URL, no second Railway service (2026-08-06,
reversed from the original separate-service plan once it was clear that just
added a second base URL to manage for no real benefit at this scale).

Reads the account universe + per-account platform IDs from Atlas
(`integrations.googleMccId`, `integrations.metaAdAccountId`), pulls real
spend from the Google Ads / Meta Marketing APIs, and exposes it over a small
REST API — reachable at `<bob-master's URL>/adspend/...` for any consumer,
in-process import for anything living in this repo (see `GoogleAdsClient`/
`MetaAdsClient` usage in `daily_go_live_audit.py` — no HTTP hop needed for
that, it's the same process).

## Endpoints (under `/adspend` once mounted)

- `GET /adspend/accounts/{customer_id}/spend?date_range=YESTERDAY` — direct
  pull by Google Ads customer ID, no Atlas involved. Useful for smoke testing.
- `GET /adspend/atlas-accounts/{atlas_id}/spend?date_range=YESTERDAY` —
  resolves the customer ID via Atlas first.
- `GET /adspend/accounts/{ad_account_id}/meta-spend?date_range=YESTERDAY` —
  same idea for Meta, direct `act_...` ad account ID.
- `GET /adspend/atlas-accounts/{atlas_id}/meta-spend?date_range=YESTERDAY` —
  resolves via Atlas's `integrations.metaAdAccountId` first.
- `date_range` — one of `TODAY`, `YESTERDAY`, `LAST_7_DAYS`, `LAST_14_DAYS`,
  `LAST_30_DAYS`, `THIS_MONTH`, `LAST_MONTH`, or any `LAST_N_DAYS` — Google
  maps these to GAQL literals or an explicit `BETWEEN`; Meta maps them to
  `date_preset` or an explicit `time_range`. Same literal strings work for
  both endpoints.

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

## Generating META_ACCESS_TOKEN

A System User access token from Business Manager — no OAuth dance, and it
doesn't expire on its own (confirmed via `/debug_token`, 2026-08-06:
`expires_at: 0`).

1. **business.facebook.com/settings** → confirm the right Business Manager is
   active → **Users → System Users** → pick or create one (Admin role is
   simplest).
2. **Business Settings → Accounts → Apps** → make sure the app is listed (Add
   → Add an App ID if not) → assign the System User to it with a role. This
   step is easy to miss — without it, token generation shows no permission
   checkboxes at all.
3. Back on the System User's page → **Generate New Token** → select the app
   → check **`ads_read`** (and optionally `business_management`) → generate.
   Meta shows the token once — copy it immediately.
4. **Separately, and easy to miss**: generating the token is not the same as
   granting the System User access to any specific ad account. Under
   **Business Settings → Accounts → Ad Accounts**, each client ad account
   needs the System User added via "Assign Partners/People" (or, more
   broadly, granting Business-level access covers everything the Business
   owns). Confirmed the hard way, 2026-08-06: a freshly generated token with
   valid `ads_read` scope still 401s per-account until this step is done —
   check coverage before assuming a 401 is a code bug:

   ```python
   from adspend.meta_ads_client import MetaAdsClient
   # /me/adaccounts lists every ad account this token can actually see
   ```

## Deploying

Nothing separate to do — it deploys whenever bob-master does (same
`requirements.txt`, same Procfile, same Railway service). Just make sure
`GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`,
`GOOGLE_ADS_REFRESH_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID`, and
`META_ACCESS_TOKEN` (plus `META_APP_ID`/`META_APP_SECRET`/
`META_BUSINESS_MANAGER_ID`, used only for token inspection) are set on
bob-master's *existing* Railway service alongside everything else
(`ATLAS_API_KEY` is already there).

## Known limitations (flagged, not blocking)

- **Meta System User coverage is partial** (as of 2026-08-06): the token only
  has access granted to 5 of the 56 Atlas accounts with a `metaAdAccountId`
  on file. The other 51 soft-fail (`meta_ads_live_ok=False` in
  `context_gather_json`) until access is broadened in Business Manager — see
  the token-generation steps above.
- **Meta conversions are not computed.** Unlike Google Ads' single
  `metrics.conversions` field, Meta's insights `actions` field is a
  heterogeneous list that varies by campaign objective (leads,
  purchases, messages, engagement, ...) with no one `action_type` that means
  "conversion" across all of them. `conversions`/`conversions_value` are
  hardcoded to `0.0` for every Meta campaign rather than guessed at — revisit
  if real Meta conversion tracking is wanted.
