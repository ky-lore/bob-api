"""
adspend — ad-spend pull package (2026-08-06). Deliberately self-contained
(own config, own thin clients, no import dependency on app/) even though it's
mounted at /adspend on bob-master's own FastAPI app rather than deployed
separately (see app/main.py, adspend/README.md) — that stays a plain
mount-point decision, not a reason to couple the two codebases.

Four ways in, Google and Meta each with the same two shapes:
  - /accounts/{customer_id}/spend — direct Google Ads customer ID, for smoke
    testing against a known ID without going through Atlas at all.
  - /atlas-accounts/{atlas_id}/spend — resolves the customer ID from Atlas's
    per-account integrations.googleMccId first (confirmed against real Atlas
    data, 2026-08-06: despite the field's name, it's the client's own Google
    Ads *customer* ID, a child account under the shared MCC in
    Settings.google_ads_login_customer_id, not a second per-client MCC).
  - /accounts/{ad_account_id}/meta-spend — same idea for Meta, direct
    "act_..." ad account ID.
  - /atlas-accounts/{atlas_id}/meta-spend — resolves via Atlas's
    integrations.metaAdAccountId, which comes pre-formatted with the "act_"
    prefix already (confirmed 2026-08-06 — no parsing needed, unlike Google's
    dashed customer IDs).
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from adspend.atlas_client import AtlasClient
from adspend.google_ads_client import GoogleAdsClient, _build_date_clause
from adspend.meta_ads_client import MetaAdsClient, _build_date_params


def _validate_date_range(date_range: str) -> None:
    try:
        _build_date_clause(date_range)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _validate_meta_date_range(date_range: str) -> None:
    try:
        _build_date_params(date_range)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


app = FastAPI(title="adspend")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/accounts/{customer_id}/spend")
def get_spend_by_customer_id(customer_id: str, date_range: str = "YESTERDAY") -> dict:
    _validate_date_range(date_range)
    try:
        return GoogleAdsClient().get_account_spend(customer_id, date_range)
    except Exception as exc:
        raise HTTPException(502, f"Google Ads pull failed: {exc}") from exc


@app.get("/atlas-accounts/{atlas_id}/spend")
def get_spend_by_atlas_id(atlas_id: str, date_range: str = "YESTERDAY") -> dict:
    _validate_date_range(date_range)

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


@app.get("/accounts/{ad_account_id}/meta-spend")
def get_meta_spend_by_ad_account_id(ad_account_id: str, date_range: str = "YESTERDAY") -> dict:
    _validate_meta_date_range(date_range)
    try:
        return MetaAdsClient().get_account_spend(ad_account_id, date_range)
    except Exception as exc:
        raise HTTPException(502, f"Meta Ads pull failed: {exc}") from exc


@app.get("/atlas-accounts/{atlas_id}/meta-spend")
def get_meta_spend_by_atlas_id(atlas_id: str, date_range: str = "YESTERDAY") -> dict:
    _validate_meta_date_range(date_range)

    accounts = AtlasClient().get_all_accounts()
    account = next((a for a in accounts if a.get("id") == atlas_id), None)
    if account is None:
        raise HTTPException(404, f"No Atlas account with id {atlas_id!r}")

    ad_account_id = (account.get("integrations") or {}).get("metaAdAccountId")
    if not ad_account_id:
        raise HTTPException(404, f"Atlas account {atlas_id!r} has no metaAdAccountId set")

    try:
        result = MetaAdsClient().get_account_spend(ad_account_id, date_range)
    except Exception as exc:
        raise HTTPException(502, f"Meta Ads pull failed: {exc}") from exc

    result["atlas_id"] = atlas_id
    result["company_name"] = account.get("companyName")
    return result
