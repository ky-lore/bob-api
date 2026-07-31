"""
Tests against real card titles pulled from the sandboxed Retention pipeline
via GET /admin/clickup/retention-sample.
"""
from app.tasks.matching import find_best_match, normalize
from app.tasks.retention_check import extract_retention_candidate_name, is_administrative_card


def test_is_administrative_card_filters_template_and_demo_cards():
    assert is_administrative_card("[EXAMPLE] CANCEL — Acme Plumbing — $2,500") is True
    assert is_administrative_card("📋 TEMPLATE — copy this for every request (do not close)") is True
    assert is_administrative_card("🔧 KYLE — Set up & own the Retention pipeline management view") is True


def test_is_administrative_card_false_for_real_clients():
    assert is_administrative_card("BluePoint Pools") is False
    assert is_administrative_card("[CHURNED] LG Electric") is False


def test_extract_retention_candidate_name_no_prefix():
    assert extract_retention_candidate_name("BluePoint Pools") == "BluePoint Pools"
    assert extract_retention_candidate_name("His Hands Plumbing Services") == "His Hands Plumbing Services"


def test_extract_retention_candidate_name_bracket_prefix():
    assert extract_retention_candidate_name("[CHURNED] LG Electric") == "LG Electric"
    assert extract_retention_candidate_name("[Chruned]Prime Lux Floors") == "Prime Lux Floors"


def test_extract_retention_candidate_name_bracket_plus_dollar_and_parenthetical():
    assert extract_retention_candidate_name("[SAVE] Amaral's Construction — $3,500 (ghosting)") == "Amaral's Construction"
    assert (
        extract_retention_candidate_name("[SAVE] Prestige Builders & Design — $2,500 (chargeback + free month burned)")
        == "Prestige Builders & Design"
    )


def test_extract_retention_candidate_name_trailing_narrative_after_dash():
    assert (
        extract_retention_candidate_name("[CHURNED] KHX Construction (Hector Soto) — cancelled after ~1 week")
        == "KHX Construction"
    )


def test_extract_retention_candidate_name_comp_prefix_with_promo_detail():
    assert (
        extract_retention_candidate_name("[COMP] Roberts Garage Doors — 4 months free (Houston event promo)")
        == "Roberts Garage Doors"
    )


def test_extract_retention_candidate_name_leading_dash_after_bracket():
    assert extract_retention_candidate_name("[SAVE] - Vera Plumbing and Drain (creative complaint)") == "Vera Plumbing and Drain"


def test_find_best_match_against_retention_cards_uses_retention_extractor():
    cards = [
        {"id": "1", "name": "[CHURNED] LG Electric"},
        {"id": "2", "name": "[COMP] Roberts Garage Doors — 4 months free (Houston event promo)"},
    ]
    result = find_best_match("Roberts Garage Doors", cards, name_extractor=extract_retention_candidate_name)
    assert result.confidence == "exact"
    assert result.card_id == "2"


def test_administrative_cards_excluded_before_matching():
    # Real scenario: the [EXAMPLE] demo card's fake client "Acme Plumbing"
    # must never be offered as a match candidate to a real "Acme Plumbing".
    all_cards = [
        {"id": "1", "name": "[EXAMPLE] CANCEL — Acme Plumbing — $2,500"},
        {"id": "2", "name": "BluePoint Pools"},
    ]
    real_cards = [c for c in all_cards if not is_administrative_card(c["name"])]
    assert real_cards == [{"id": "2", "name": "BluePoint Pools"}]
