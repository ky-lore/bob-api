"""
adspend — standalone ad-spend pull service (2026-08-06). Deliberately kept
separate from bob-master's app/ (the LLM+write service) even though both live
in this one repo for now: this is meant to be reused by other projects later,
so it has its own config, its own thin clients, and no import dependency on
app/. Deployed as its own Railway service with Root Directory set to adspend/
(see Procfile).

Two ways in:
  - /accounts/{customer_id}/spend — direct Google Ads customer ID, for smoke
    testing against a known ID without going through Atlas at all.
  - /atlas-accounts/{atlas_id}/spend — resolves the customer ID from Atlas's
    per-account integrations.googleMccId first. NOTE (2026-08-06, unconfirmed):
    despite the field's name, it's being treated here as the client's own
    Google Ads *customer* ID (a child account under the shared MCC in
    Settings.google_ads_login_customer_id), not a second per-client MCC —
    that's the only interpretation that lines up with how Google Ads access
    actually works. Confirm this against real Atlas data before trusting it.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from adspend.atlas_client import AtlasClient
from adspend.google_ads_client import ALLOWED_DATE_RANGES, GoogleAdsClient

app = FastAPI(title="adspend")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/accounts/{customer_id}/spend")
def get_spend_by_customer_id(customer_id: str, date_range: str = "YESTERDAY") -> dict:
    if date_range not in ALLOWED_DATE_RANGES:
        raise HTTPException(400, f"date_range must be one of {sorted(ALLOWED_DATE_RANGES)}")
    try:
        return GoogleAdsClient().get_account_spend(customer_id, date_range)
    except Exception as exc:
        raise HTTPException(502, f"Google Ads pull failed: {exc}") from exc


@app.get("/atlas-accounts/{atlas_id}/spend")
def get_spend_by_atlas_id(atlas_id: str, date_range: str = "YESTERDAY") -> dict:
    if date_range not in ALLOWED_DATE_RANGES:
        raise HTTPException(400, f"date_range must be one of {sorted(ALLOWED_DATE_RANGES)}")

    accounts = AtlasClient().get_all_accounts()
    account = next((a for a in accounts if a.get("id") == atlas_id), None)
    if account is None:
        raise HTTPException(404, f"No Atlas account with id {atlas_id!r}")

    customer_id = (account.get("integrations") or {}).get("googleMccId")
    if not customer_id:
        raise HTTPException(404, f"Atlas account {atlas_id!r} has no googleMccId set")

    try:
        result = GoogleAdsClient().get_account_spend(customer_id, date_range)
    except Exception as exc:
        raise HTTPException(502, f"Google Ads pull failed: {exc}") from exc

    result["atlas_id"] = atlas_id
    result["company_name"] = account.get("companyName")
    return result
