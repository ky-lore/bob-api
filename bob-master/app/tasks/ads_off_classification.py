"""
Classifies an account into the "Ads off — who's dark and why" buckets from the
reference dashboard (golive-pipeline-dashboard.pdf, provided 2026-07-31):
  - should_be_on_but_dark: card says live/optimizations, but BOTH platforms
    show no real spend (including a platform with no heartbeat row at all —
    the reference dashboard's own "Shelby Plumbing" example has no Meta row
    whatsoever and is still listed here, not silently skipped)
  - campaigns_on_zero_spend: a platform has enabled campaigns but $0 spend
    (per platform — an account can appear for one or both platforms)
  - unsettled: Meta "Account status" is UNSETTLED (payment blocking delivery)
  - verified_off: retention shows churned/cancelled AND heartbeat confirms no
    real spend on either platform

An account can land in more than one bucket at once — the reference dashboard
does this too (an unsettled account can simultaneously show in the zero-spend
table with its own per-platform note).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AdsOffClassification:
    should_be_on_but_dark: bool = False
    campaigns_on_zero_spend: list[str] = field(default_factory=list)  # platform labels
    unsettled: bool = False
    verified_off: bool = False


_LIVE_LIKE_STAGES = {"live", "optimizations"}


def classify_ads_off(
    *,
    google_row=None,
    meta_row=None,
    card_status: str | None,
    retention_status: str | None,
    churned_statuses: set[str],
) -> AdsOffClassification:
    result = AdsOffClassification()
    platform_rows = [("Google Ads", google_row), ("Meta", meta_row)]

    for label, row in platform_rows:
        if row is not None and row.enabled_campaigns > 0 and row.total_spend == 0:
            result.campaigns_on_zero_spend.append(label)

    if meta_row is not None and meta_row.account_status == "UNSETTLED":
        result.unsettled = True

    both_dark = not any(row is not None and (row.am_build_spend > 0 or row.lsa_spend > 0) for _, row in platform_rows)
    is_churned = retention_status is not None and retention_status in churned_statuses

    if is_churned and both_dark:
        result.verified_off = True
    elif (card_status or "").lower() in _LIVE_LIKE_STAGES and both_dark:
        result.should_be_on_but_dark = True

    return result
