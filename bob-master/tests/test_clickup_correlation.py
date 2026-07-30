from app.tasks.clickup_correlation import (
    days_since_created,
    extract_manual_day_count,
    identify_package,
    resolve_day_count,
)


def test_identify_package_prefers_known_tag():
    assert identify_package(["pkg-web"], "anything") == "pkg-web"


def test_identify_package_reads_real_legacy_markers():
    assert identify_package([], "3) Emberline Shower Enclosures [MKTG] Marketing") == "pkg-mktg"
    assert identify_package([], "2) Development · ColdRiite Walk-Ins · Day 374 🔴 [WEB pkg] — OLDEST open build") == "pkg-web"
    assert identify_package([], "2) Development · MM Cellar Systems · Day 88 · [WEB+VIDEO pkg — no 14-day ads clock]") == "pkg-web-custom"
    assert identify_package([], "1) Onboarding · Verterra Outdoor Living · Day 80 · [FREE 6-MO PROMO winner — no ads clock]") == "pkg-free-promo"


def test_identify_package_unrecognized_marker_falls_through_honestly():
    # Real card: "[MKTG — SEO pkg]" isn't one of SKILL.md's four named legacy
    # patterns as a compound — "mktg" alone still matches, which is a faithful
    # reading of the literal text present, not a guess at the SEO add-on.
    assert identify_package([], "1) Onboarding · The Alliance 247 · signed 7/14 · [MKTG — SEO pkg]") == "pkg-mktg"


def test_identify_package_no_tag_no_marker_is_unidentified():
    assert identify_package([], "Ad creative update") == "unidentified"
    assert identify_package(["newclientgolivetracker"], "[Live]His Hands Plumbing") == "unidentified"


def test_extract_manual_day_count_reads_real_examples():
    assert extract_manual_day_count("2) Development · ColdRiite Walk-Ins · Day 374 🔴 [WEB pkg]") == 374
    assert extract_manual_day_count("1) Onboarding · Roberts Garage Doors · Day 9 🟡 (no domain exists)") == 9
    assert extract_manual_day_count("[Live]His Hands Plumbing") is None


def test_days_since_created_from_real_epoch_ms():
    # 1785426730125 ms is the real date_created from the Pavement Enforcement
    # card pulled this session — must be a small positive number of days, not
    # negative and not absurdly large (i.e. the ms->s conversion is correct).
    days = days_since_created("1785426730125")
    assert 0 <= days < 60


def test_resolve_day_count_prefers_manual_over_computed():
    # ColdRiite's real date_created is recent (card recreated/carried forward)
    # but the title's "Day 374" reflects the true, much older signing date —
    # exactly the case this function exists to handle correctly.
    result = resolve_day_count("2) Development · ColdRiite Walk-Ins · Day 374 🔴 [WEB pkg]", "1784213113695")
    assert result == 374


def test_resolve_day_count_falls_back_to_computed_when_no_manual_count():
    result = resolve_day_count("[Live]His Hands Plumbing", "1783752041572")
    assert result == days_since_created("1783752041572")
