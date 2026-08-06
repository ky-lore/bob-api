"""
Classifies an account into the "Ads off — who's dark and why" buckets from the
reference dashboard (golive-pipeline-dashboard.pdf). Atlas-only, cross-platform
(2026-08-06 revision — Google Ads + Meta, cross-platform):
  - should_be_on_but_dark: Atlas stage says live/optimizations, but every
    platform this account actually has data for shows $0 spend. Cross-platform
    on purpose (Bob: "a client live via Meta no longer gets flagged for a $0
    legacy campaign on Google") -- an account live via Meta but dark on
    Google must NOT be flagged just because Google is dark. An account with
    no platform data at all (website-only, or no ID mapped, or every pull
    failed) is skipped entirely -- no data to classify, not confirmed dark.
  - campaigns_on_zero_spend: per platform, independently -- Google Ads and/or
    Meta can each have enabled campaigns but $0 spend; an account can appear
    for one or both.
  - billing_unsettled: PLACEHOLDER (Bob, 2026-08-06: "that'll take a while to
    reconcile") — always False. No real billing/payment signal is wired in
    yet. Kept as a real field so the dashboard's shape already has a place
    for it once it exists.
  - verified_off: Atlas stage == "closed", full stop — no independent
    ad-platform re-verification. Per Bob: "assume Atlas will always have
    perfect data regarding account status."

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


def classify_ads_off(
    *,
    stage: str | None,
    google_ads: dict[str, Any] | None,
    meta_ads: dict[str, Any] | None = None,
) -> AdsOffClassification:
    result = AdsOffClassification()
    available = [(label, data) for label, data in (("Google Ads", google_ads), ("Meta", meta_ads)) if data is not None]

    for label, data in available:
        if data["enabled_campaigns"] > 0 and data["spend"] == 0:
            result.campaigns_on_zero_spend.append(label)

    if available and (stage or "").lower() in _LIVE_LIKE_STAGES and all(data["spend"] == 0 for _, data in available):
        result.should_be_on_but_dark = True

    result.verified_off = (stage or "").lower() == "closed"
    return result
