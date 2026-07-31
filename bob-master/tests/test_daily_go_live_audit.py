"""
Smoke tests for the deterministic pieces of daily_go_live_audit.py — the parts
that don't need real ClickUp/GHL/Slack/Drive credentials. This is the first
test coverage this system has ever had (see docs/PROJECT-BRIEF-FOR-NEW-DEV.md
§10 item 5) — extend this as the TODO-marked correlation logic gets built out,
rather than leaving it as the only tests forever.
"""
from datetime import datetime

from app.tasks.daily_go_live_audit import (
    HeartbeatRow,
    evaluate_package_clock,
    is_live,
    legacy_only_spend_flag,
    parse_heartbeat_rows,
)


def _row(**kwargs) -> HeartbeatRow:
    defaults = dict(
        account_name="Test Client",
        enabled_campaigns=1,
        am_build_spend=0.0,
        legacy_spend=0.0,
        lsa_spend=0.0,
        checked_at=datetime.min,
    )
    defaults.update(kwargs)
    return HeartbeatRow(**defaults)


def test_is_live_true_for_am_build_spend():
    assert is_live(_row(am_build_spend=42.0)) is True


def test_is_live_true_for_lsa_spend():
    assert is_live(_row(lsa_spend=10.0)) is True


def test_is_live_false_for_legacy_only_spend():
    assert is_live(_row(legacy_spend=99.0)) is False


def test_legacy_only_spend_flag():
    assert legacy_only_spend_flag(_row(legacy_spend=99.0)) is True
    assert legacy_only_spend_flag(_row(am_build_spend=10.0, legacy_spend=99.0)) is False


def test_parse_heartbeat_rows_matches_by_header_name():
    raw = [
        ["Account", "Enabled Campaigns", "AM-Build Spend", "Legacy Spend", "LSA Spend", "Checked At"],
        ["Acme Co", "2", "150.00", "0", "0", "2026-07-30T08:00:00"],
    ]
    rows = parse_heartbeat_rows(raw)
    assert len(rows) == 1
    assert rows[0].account_name == "Acme Co"
    assert rows[0].am_build_spend == 150.0


def test_parse_heartbeat_rows_reads_real_lsa_spend_column_not_the_enabled_flag():
    # Real header pulled from the live sheet via GET /admin/heartbeat/headers.
    # "Enabled LSA" is a yes/no flag; the real dollar figure is "Spend
    # yesterday (LSA)". Grabbing the wrong one silently zeroed lsa_spend for
    # every account and misclassified LSA-only-live clients as legacy-only.
    header = [
        "Checked at", "Account name", "CID", "Enabled campaigns", "Enabled LSA",
        "Spend yesterday (ads)", "Spend yesterday (LSA)", "Spend today (ads)",
        "Spend today (LSA)", "AM-BUILD spend yest", "Legacy spend yest", "Status", "Flag",
    ]
    raw = [
        header,
        ["2026-07-30 14:26:00", "Acme Co", "123", "3", "TRUE", "50", "75.50", "10", "20", "0", "40", "", ""],
    ]
    rows = parse_heartbeat_rows(raw)
    assert len(rows) == 1
    assert rows[0].lsa_spend == 75.50
    assert rows[0].am_build_spend == 0.0
    assert rows[0].legacy_spend == 40.0
    assert is_live(rows[0]) is True  # live via LSA spend, despite legacy spend also being present


def test_package_clock_mktg_flags_at_day_14_and_21():
    assert evaluate_package_clock("pkg-mktg", 10, is_live=False) is None
    assert "Day 14" in evaluate_package_clock("pkg-mktg", 14, is_live=False)
    assert "Day 21" in evaluate_package_clock("pkg-mktg", 21, is_live=False)
    assert evaluate_package_clock("pkg-mktg", 21, is_live=True) is None


def test_package_clock_web_custom_needs_eta():
    assert "no dated ETA" in evaluate_package_clock("pkg-web-custom", 100, is_live=False, has_eta=False)
    assert evaluate_package_clock("pkg-web-custom", 100, is_live=False, has_eta=True, eta_passed=False) is None


def test_package_clock_free_promo_never_flags():
    assert evaluate_package_clock("pkg-free-promo", 999, is_live=False) is None


def test_package_clock_unidentified_flags_for_tagging():
    result = evaluate_package_clock("unknown", 5, is_live=False)
    assert "unidentified" in result


def test_parse_heartbeat_rows_skips_blank_account_name():
    # Real sheet had subtotal/spacer rows with an empty account column, which
    # produced garbled "stale for  (checked_at ...)" entries with no name.
    raw = [
        ["Account", "Enabled Campaigns", "AM-Build Spend", "Legacy Spend", "LSA Spend", "Checked At"],
        ["", "0", "0", "0", "0", "2026-07-30T14:26:00"],
        ["   ", "0", "0", "0", "0", "2026-07-30T14:26:00"],
        ["Acme Co", "2", "150.00", "0", "0", "2026-07-30T14:26:00"],
    ]
    rows = parse_heartbeat_rows(raw)
    assert len(rows) == 1
    assert rows[0].account_name == "Acme Co"


def test_is_stale_interprets_naive_timestamp_as_pacific_not_utc():
    # The real bug: comparing a Pacific-time "Checked at" value directly against
    # UTC made every fresh row look ~7-8h old and get flagged stale on the first
    # live run. A timestamp genuinely 1 hour old in Pacific time must not be stale.
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from app.integrations.google_drive import GoogleDriveClient

    now_pacific = datetime.now(ZoneInfo("America/Los_Angeles"))
    one_hour_ago_naive = (now_pacific - timedelta(hours=1)).replace(tzinfo=None)

    assert GoogleDriveClient.is_stale(one_hour_ago_naive) is False


def test_is_stale_is_same_calendar_day_not_a_rolling_window():
    # Per GoLive_Audit_Dev_Handover_Brief.md §1: the original checks "Checked
    # at" is from *today* (same Pacific calendar day), not a rolling N-hour
    # window — the source script refreshes on a staggered hourly cycle (some
    # rows 10:27, others 13:27, both normal), so an early-in-the-day row that's
    # still genuinely fresh must not be flagged stale just because it's >3h old.
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    from app.integrations.google_drive import GoogleDriveClient

    now_pacific = datetime.now(ZoneInfo("America/Los_Angeles"))
    earlier_today_naive = datetime.combine(now_pacific.date(), time(0, 1))
    assert GoogleDriveClient.is_stale(earlier_today_naive) is False


def test_is_stale_flags_genuinely_old_or_missing_timestamps():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from app.integrations.google_drive import GoogleDriveClient

    now_pacific = datetime.now(ZoneInfo("America/Los_Angeles"))
    yesterday_naive = (now_pacific - timedelta(days=1)).replace(tzinfo=None)

    assert GoogleDriveClient.is_stale(yesterday_naive) is True
    assert GoogleDriveClient.is_stale(datetime.min) is True
