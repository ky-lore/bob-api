"""
Tests classify_ads_off against the new Atlas-only, single-platform (Google
Ads) shape (2026-08-06) -- heartbeat rows and the retention cross-check are
gone; stage comes from Atlas, spend/campaigns come from a real Google Ads
pull (see live_google_ads_spend in daily_go_live_audit.py).
"""
from app.tasks.ads_off_classification import classify_ads_off


def _google_ads(spend=0.0, enabled_campaigns=0):
    return {"spend": spend, "enabled_campaigns": enabled_campaigns, "impressions": 0, "clicks": 0, "conversions": 0.0, "campaigns": []}


def test_should_be_on_but_dark_when_stage_live_and_zero_spend():
    result = classify_ads_off(stage="live", google_ads=_google_ads(spend=0.0, enabled_campaigns=1))
    assert result.should_be_on_but_dark is True


def test_should_be_on_but_dark_false_when_stage_not_live_like():
    result = classify_ads_off(stage="onboarding", google_ads=_google_ads(spend=0.0))
    assert result.should_be_on_but_dark is False


def test_should_be_on_but_dark_false_when_no_google_ads_data_at_all():
    # No googleMccId on file, or the pull failed -- we don't have data to
    # classify this account, so it must NOT be assumed dark.
    result = classify_ads_off(stage="live", google_ads=None)
    assert result.should_be_on_but_dark is False


def test_campaigns_on_zero_spend_when_enabled_but_no_real_spend():
    result = classify_ads_off(stage="live", google_ads=_google_ads(spend=0.0, enabled_campaigns=83))
    assert result.campaigns_on_zero_spend == ["Google Ads"]


def test_campaigns_on_zero_spend_empty_when_actually_spending():
    result = classify_ads_off(stage="live", google_ads=_google_ads(spend=42.5, enabled_campaigns=3))
    assert result.campaigns_on_zero_spend == []


def test_campaigns_on_zero_spend_empty_when_no_enabled_campaigns():
    result = classify_ads_off(stage="live", google_ads=_google_ads(spend=0.0, enabled_campaigns=0))
    assert result.campaigns_on_zero_spend == []


def test_billing_unsettled_is_always_false_placeholder():
    # Bob, 2026-08-06: "have billing placeholder for now... that'll take a
    # while to reconcile" -- no real signal feeds this yet.
    assert classify_ads_off(stage="live", google_ads=_google_ads(spend=0.0, enabled_campaigns=1)).billing_unsettled is False
    assert classify_ads_off(stage="live", google_ads=None).billing_unsettled is False


def test_verified_off_trusts_atlas_stage_directly_no_cross_check():
    # "Assume Atlas will always have perfect data regarding account status"
    # -- no independent ad-platform re-verification needed, and it works even
    # with no Google Ads data at all.
    result = classify_ads_off(stage="closed", google_ads=None)
    assert result.verified_off is True


def test_verified_off_false_for_live_stage():
    result = classify_ads_off(stage="live", google_ads=_google_ads(spend=0.0))
    assert result.verified_off is False


def test_an_account_can_land_in_both_should_be_on_and_zero_spend_buckets_at_once():
    result = classify_ads_off(stage="live", google_ads=_google_ads(spend=0.0, enabled_campaigns=2))
    assert result.should_be_on_but_dark is True
    assert result.campaigns_on_zero_spend == ["Google Ads"]
