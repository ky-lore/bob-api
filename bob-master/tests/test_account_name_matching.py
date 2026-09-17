from app.tasks.account_name_matching import best_match, normalize


def test_normalize_strips_emoji_punctuation_and_legal_suffixes():
    assert normalize("🏠 Absolute Best Service Plumbing, Inc.") == "absolute best service plumbing"


def test_normalize_strips_parenthetical_asides():
    assert normalize("Alejandro's Remodeling LLC (dba Something Else)") == "alejandro s remodeling"


def test_normalize_collapses_ampersand_and_hvac_synonym():
    assert normalize("J&C Heating & Air Conditioning") == "j and c hvac"


def test_best_match_returns_exact_match_at_full_confidence():
    candidates = {"Acme Co": normalize("Acme Co"), "Beta LLC": normalize("Beta LLC")}
    name, score = best_match(normalize("Acme Co"), candidates)
    assert name == "Acme Co"
    assert score == 1.0


def test_best_match_finds_a_containment_match():
    candidates = {"Good Neighbor": normalize("Good Neighbor")}
    name, score = best_match(normalize("Good Neighbor Fence"), candidates)
    assert name == "Good Neighbor"
    assert score >= 0.82


def test_best_match_returns_none_for_empty_candidates():
    name, score = best_match(normalize("Acme Co"), {})
    assert name is None
    assert score == 0.0


def test_best_match_low_score_for_unrelated_names():
    candidates = {"Drain Force Plumbing": normalize("Drain Force Plumbing")}
    name, score = best_match(normalize("Mariachi Corazon de Maria"), candidates)
    assert score <= 0.4


def test_normalize_strips_zoom_topic_boilerplate():
    # Real miss, 2026-09-17: without stripping this, "Speedee X Advanced
    # Marketers" scored too low against "Speedee Drains and Plumbing" --
    # the boilerplate diluted the token overlap enough to miss a real match.
    assert normalize("Speedee X Advanced Marketers") == "speedee"
    assert normalize("Advanced Marketers X Magic Snake Pro | Weekly Meeting") == "magic snake pro"


def test_short_client_nickname_matches_the_full_company_name_after_stripping_boilerplate():
    candidates = {"Speedee Drains and Plumbing": normalize("Speedee Drains and Plumbing")}
    name, score = best_match(normalize("Speedee X Advanced Marketers"), candidates)
    assert name == "Speedee Drains and Plumbing"
    assert score >= 0.82


def test_generic_topic_with_no_real_company_name_does_not_land_a_coincidental_match():
    # Real false positive, 2026-09-17: "Introduction" (no company name at
    # all) scored 0.65 against an unrelated real company purely from
    # short-string character overlap -- above the old 0.55 floor, below the
    # 0.82 one this task's _MIN_MATCH_CONFIDENCE now uses.
    candidates = {"Vizeon Construction": normalize("Vizeon Construction")}
    name, score = best_match(normalize("Introduction"), candidates)
    assert score < 0.82
