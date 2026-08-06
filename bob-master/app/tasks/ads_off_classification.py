"""
Classifies an account into the "Ads off — who's dark and why" buckets from the
reference dashboard (golive-pipeline-dashboard.pdf). Rewritten 2026-08-06 for
the Atlas-only, heartbeat-free pipeline:
  - should_be_on_but_dark: Atlas stage says live/optimizations, but Google Ads
    shows no real spend. Only evaluated when google_ads is present — an
    account with no googleMccId or a failed pull is skipped, not assumed dark.
  - campaigns_on_zero_spend: Google Ads has enabled campaigns but $0 spend
    (kept as a list of platform labels, not a bare bool, so Meta can join
    this list later without a shape change)
  - billing_unsettled: PLACEHOLDER (Bob, 2026-08-06: "that'll take a while to
    reconcile") — always False. No real billing/payment signal is wired in
    yet (no GHL field, no Google Ads billing-status query). Kept as a real
    field so the dashboard's shape already has a place for it once it exists.
  - verified_off: Atlas stage == "closed", full stop — no independent
    ad-platform re-verification. Per Bob: "assume Atlas will always have
    perfect data regarding account status." This replaces the old
    retention-pipeline cross-check entirely.

An account can land in more than one bucket at once — e.g. should_be_on_but_dark
and campaigns_on_zero_spend can both be true for the same account.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_LIVE_LIKE_STAGES = {"live", "optimizations"}


@dataclass
class AdsOffClassification:
    should_be_on_but_dark: bool = False
    campaigns_on_zero_spend: list[str] = field(default_factory=list)  # platform labels
    billing_unsettled: bool = False
    verified_off: bool = False


def classify_ads_off(*, stage: str | None, google_ads: dict[str, Any] | None) -> AdsOffClassification:
    result = AdsOffClassification()

    if google_ads is not None:
        if google_ads["enabled_campaigns"] > 0 and google_ads["spend"] == 0:
            result.campaigns_on_zero_spend.append("Google Ads")
        if (stage or "").lower() in _LIVE_LIKE_STAGES and google_ads["spend"] == 0:
            result.should_be_on_but_dark = True

    result.verified_off = (stage or "").lower() == "closed"
    return result
