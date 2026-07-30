"""
Tests against real card titles pulled from the sandboxed Go-Live board via
GET /admin/clickup/go-live-sample — not synthetic examples, the actual mess.
"""
from app.tasks.matching import extract_candidate_name, find_best_match, normalize


def test_extract_candidate_name_stage_word_and_dot_form():
    title = "1) Onboarding · Pavement Enforcement (Diego) · signed 7/21 · [package marker missing] — ⚠️ verbal promo NOT in signed agreement"
    assert extract_candidate_name(title) == "Pavement Enforcement"


def test_extract_candidate_name_strips_parenthetical_location():
    title = "1) Onboarding · MGS Air Conditioning & Heating (Tulare, CA) · channel created 7/28 · [package marker missing — confirm in GHL]"
    assert extract_candidate_name(title) == "MGS Air Conditioning & Heating"


def test_extract_candidate_name_stage_number_only_no_stage_word():
    # "3) Emberline Shower Enclosures [MKTG] Marketing" — no stage-word/dot at all
    title = "3) Emberline Shower Enclosures [MKTG] Marketing"
    assert extract_candidate_name(title) == "Emberline Shower Enclosures"


def test_extract_candidate_name_dev_stage_with_day_count_and_dash():
    title = "2) Development · ColdRiite Walk-Ins · Day 374 🔴 [WEB pkg] — OLDEST open build, finish ASAP (Chris 7/16)"
    assert extract_candidate_name(title) == "ColdRiite Walk-Ins"


def test_extract_candidate_name_bracket_prefix_no_space():
    assert extract_candidate_name("[Live]His Hands Plumbing") == "His Hands Plumbing"


def test_extract_candidate_name_bracket_prefix_with_leading_emoji():
    title = "[AM] 🚨 Sierra Trimlight — wants to CANCEL + card declining with us. Save attempt / collect before next cycle"
    assert extract_candidate_name(title) == "Sierra Trimlight"


def test_extract_candidate_name_cancelled_stage_with_em_dash():
    title = "5) Cancelled · Ram Dumpster — client cancelled; TURN GOOGLE ADS OFF (campaigns still enabled)"
    assert extract_candidate_name(title) == "Ram Dumpster"


def test_extract_candidate_name_non_client_card_returns_something_that_wont_match():
    # Not every card is a client — this one must not silently match a real account.
    assert extract_candidate_name("Ad creative update") == "Ad creative update"


def test_normalize_handles_case_and_legacy_suffixes():
    assert normalize("Roof City Inc") == normalize("roof city")


def test_find_best_match_exact():
    cards = [{"id": "1", "name": "[Live]His Hands Plumbing"}, {"id": "2", "name": "[Live]Reel Electric"}]
    result = find_best_match("His Hands Plumbing", cards)
    assert result.confidence == "exact"
    assert result.card_id == "1"


def test_find_best_match_uses_alias_table_for_real_near_duplicate():
    # The actual pair flagged in SKILL.md and confirmed present on the real board.
    cards = [{"id": "1", "name": "[Live]Roof City Professionals"}]
    aliases = {normalize("Roof City Inc - CC"): normalize("Roof City Professionals")}
    result = find_best_match("Roof City Inc - CC", cards, aliases=aliases)
    assert result.confidence == "alias"
    assert result.card_id == "1"


def test_find_best_match_returns_none_for_unrelated_card():
    cards = [{"id": "1", "name": "Ad creative update"}]
    result = find_best_match("Andy's Pools Inc", cards)
    assert result.confidence == "none"
    assert result.card_id is None


def test_find_best_match_flags_close_but_unconfirmed_as_ambiguous():
    # Real false-positive from the first live run: different companies sharing
    # only the generic word "Electric", scoring 0.83 — inside the ambiguous
    # band, correctly NOT auto-applied.
    cards = [{"id": "1", "name": "[Live]Reel Electric"}]
    result = find_best_match("LG Electric", cards)
    assert result.confidence == "ambiguous"
    assert result.card_id == "1"


def test_find_best_match_high_confidence_for_formatting_only_differences():
    # Real high-scoring correct match from the first live run (0.98) —
    # auto-applied, not flagged for confirmation.
    cards = [{"id": "1", "name": "1) Onboarding · Roberts Garage Doors · Day 9"}]
    result = find_best_match("Robert's Garage Doors", cards)
    assert result.confidence == "high"
    assert result.card_id == "1"
