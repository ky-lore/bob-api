"""
Tests built from the actual real-world examples in golive-pipeline-dashboard.pdf
(the reference dashboard), not synthetic ones — Roof City Professionals, Shelby
Plumbing, 5blox, Reel Electric, Ram Dumpster are all real named examples there.
"""
from datetime import datetime

from app.tasks.ads_off_classification import classify_ads_off
from app.tasks.daily_go_live_audit import HeartbeatRow

CHURNED = {"churned x"}


def _row(**kwargs) -> HeartbeatRow:
    defaults = dict(
        account_name="X", enabled_campaigns=0, am_build_spend=0.0, legacy_spend=0.0,
        lsa_spend=0.0, checked_at=datetime.min, account_status="",
    )
    defaults.update(kwargs)
    return HeartbeatRow(**defaults)


def test_should_be_on_but_dark_both_platforms_zero():
    # Roof City Professionals: "Card: Google Live 7/14. Heartbeat: 0 campaigns both platforms"
    result = classify_ads_off(
        google_row=_row(enabled_campaigns=0),
        meta_row=_row(enabled_campaigns=0),
        card_status="live",
        retention_status=None,
        churned_statuses=CHURNED,
    )
    assert result.should_be_on_but_dark is True


def test_should_be_on_but_dark_when_platform_row_entirely_missing():
    # Shelby Plumbing: "Card: Meta Ads Live. No matching ad account in the Meta
    # heartbeat at all" — missing row must count as dark, not be silently skipped.
    result = classify_ads_off(
        google_row=None,
        meta_row=None,
        card_status="live",
        retention_status=None,
        churned_statuses=CHURNED,
    )
    assert result.should_be_on_but_dark is True


def test_campaigns_on_zero_spend_per_platform():
    # 5blox: "Meta 83 campaigns enabled, $0 — biggest anomaly"
    result = classify_ads_off(
        google_row=None,
        meta_row=_row(enabled_campaigns=83, am_build_spend=0, legacy_spend=0, lsa_spend=0),
        card_status="pre-live check",
        retention_status=None,
        churned_statuses=CHURNED,
    )
    assert result.campaigns_on_zero_spend == ["Meta"]
    assert result.should_be_on_but_dark is False  # card isn't live/optimizations


def test_campaigns_on_zero_spend_both_platforms_independently():
    # Next Era HVAC: "Google paused-verification; Meta 1 campaign $0"
    result = classify_ads_off(
        google_row=_row(enabled_campaigns=2, am_build_spend=0, legacy_spend=0, lsa_spend=0),
        meta_row=_row(enabled_campaigns=1, am_build_spend=0, legacy_spend=0, lsa_spend=0),
        card_status="pre-live",
        retention_status=None,
        churned_statuses=CHURNED,
    )
    assert set(result.campaigns_on_zero_spend) == {"Google Ads", "Meta"}


def test_unsettled_is_meta_specific_and_independent_of_other_buckets():
    # Reel Electric: "Meta unsettled; Google legacy campaigns spending $289/day"
    result = classify_ads_off(
        google_row=_row(enabled_campaigns=3, legacy_spend=289.0),
        meta_row=_row(enabled_campaigns=1, account_status="UNSETTLED"),
        card_status="live",
        retention_status=None,
        churned_statuses=CHURNED,
    )
    assert result.unsettled is True
    # Google side is legacy-only spend, not zero -- shouldn't land in zero-spend bucket
    assert "Google Ads" not in result.campaigns_on_zero_spend
    # Meta has an enabled campaign with $0 real spend -> also zero-spend
    assert "Meta" in result.campaigns_on_zero_spend


def test_verified_off_when_churned_and_confirmed_dark():
    # Ram Dumpster: "Cancelled 7/16 — Google campaigns confirmed OFF (0 enabled)"
    result = classify_ads_off(
        google_row=_row(enabled_campaigns=0),
        meta_row=None,
        card_status="complete",
        retention_status="churned x",
        churned_statuses=CHURNED,
    )
    assert result.verified_off is True
    assert result.should_be_on_but_dark is False  # churned takes precedence, not a should-be-on alarm


def test_precision_drywall_reverse_case_not_verified_off():
    # Precision Drywall & Paint: "client says cancelled but Meta is still ON
    # and spending ~$52/day" -- must NOT be marked verified_off (it's the
    # opposite problem: still live despite claiming cancellation).
    result = classify_ads_off(
        google_row=None,
        meta_row=_row(enabled_campaigns=1, am_build_spend=52.0),
        card_status="live",
        retention_status="save attempt (48hours)",
        churned_statuses=CHURNED,
    )
    assert result.verified_off is False
    assert result.should_be_on_but_dark is False  # it's actually live via Meta, not dark
