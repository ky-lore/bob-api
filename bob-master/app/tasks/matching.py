"""
Matches a heartbeat/GHL account name to a ClickUp Go-Live board card.

Card titles are not structured data — real examples pulled from the sandbox
board (see chat history / docs/TASK-INVENTORY.md):
  "1) Onboarding · Pavement Enforcement (Diego) · signed 7/21 · [package marker missing] — ..."
  "2) Development · ColdRiite Walk-Ins · Day 374 🔴 [WEB pkg] — OLDEST open build..."
  "[Live]His Hands Plumbing"
  "[AM] 🚨 Sierra Trimlight — wants to CANCEL + card declining with us..."
  "Ad creative update"   <- not a client card at all, must not match anything

The stage-number prefix ("1) Onboarding", "2) Development") is stale relative
to the real status field (confirmed against live data — a card titled
"1) Onboarding..." can have status "development") and must not be trusted for
anything except stripping it out of the name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Stage-number prefix. Two real forms exist: "1) Onboarding · <name>" and
# "3) <name> [MKTG]" (no stage-word/dot at all) — the stage-word-and-dot part
# is optional.
_STAGE_PREFIX_RE = re.compile(r"^\d+\)\s*(?:[A-Za-z]+\s*·\s*)?", re.UNICODE)
# Bracket-prefix with no separator: "[Live]His Hands...", "[AM] 🚨 Sierra..."
_BRACKET_PREFIX_RE = re.compile(r"^\[[A-Za-z]+\]\s*", re.UNICODE)
# Leading emoji/symbols left after a bracket-prefix strip, e.g. "🚨 Sierra..."
_LEADING_SYMBOLS_RE = re.compile(r"^[^\w]+", re.UNICODE)
# First metadata separator after the name: a pipe-dot, em-dash, opening paren,
# or the start of a bracketed marker like [MKTG].
_TRAILING_METADATA_RE = re.compile(r"\s*(·|—|\(|\[).*$", re.UNICODE)

_COMMON_SUFFIXES = (" inc", " llc", " co", " corp", " company", " ltd")


def extract_candidate_name(card_title: str) -> str:
    """Best-effort extraction of the client name from a card title. Not
    guaranteed correct — genuinely ambiguous or metadata-only titles (e.g.
    "Ad creative update") return whatever's left, which callers should treat
    as low-confidence if it doesn't match anything reasonable."""
    name = card_title.strip()
    name = _STAGE_PREFIX_RE.sub("", name)
    name = _BRACKET_PREFIX_RE.sub("", name)
    name = _LEADING_SYMBOLS_RE.sub("", name)
    name = _TRAILING_METADATA_RE.sub("", name)
    return name.strip()


def identity_name(name: str) -> str:
    """No-op extractor for name sources that are already clean — e.g. Atlas's
    companyName, unlike ClickUp card titles (extract_candidate_name's job),
    needs no stage-prefix/bracket/emoji stripping at all. Using the default
    extractor against a clean name risks corrupting it (a company literally
    named "3M Roofing" could get mangled by the stage-number-prefix pattern)."""
    return name


def normalize(name: str) -> str:
    """Lowercase, drop common legal suffixes and punctuation, collapse
    whitespace — so "Roof City Inc - CC" and "Roof City Inc." compare fairly
    (though this alone does NOT solve genuine near-duplicates like "Roof City
    Inc - CC" vs "Roof City Professionals" — that needs the alias table)."""
    n = name.lower()
    n = re.sub(r"[.,'\"&]", " ", n)
    n = re.sub(r"-", " ", n)
    for suffix in _COMMON_SUFFIXES:
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    n = re.sub(r"\s+", " ", n).strip()
    return n


@dataclass
class MatchResult:
    card_id: str | None
    card_name: str | None
    confidence: str  # "exact" | "alias" | "high" | "ambiguous" | "none"
    score: float


# Calibrated against the first real run's ambiguous-match output, not guessed:
# every score >=0.85 in that batch was a correct match (formatting/casing
# differences only); the 0.72-0.85 band had a real mix — genuine matches
# (GG&N Plumbing Solutions Inc/GG&N Plumbing, 0.72) alongside false positives
# from shared generic words (LG Electric/Reel Electric 0.83, Isramar
# Construction/Amaral's Construction 0.83, Drain It/Drain Force Plumbing 0.81,
# Abs/Axxel's Plumbing 0.79). One threshold can't serve both purposes.
HIGH_CONFIDENCE_THRESHOLD = 0.85
AMBIGUOUS_THRESHOLD = 0.72


def find_best_match(
    target_name: str,
    cards: list[dict],  # each: {"id": str, "name": str}
    *,
    aliases: dict[str, str] | None = None,
    high_confidence_threshold: float = HIGH_CONFIDENCE_THRESHOLD,
    ambiguous_threshold: float = AMBIGUOUS_THRESHOLD,
    name_extractor=extract_candidate_name,
) -> MatchResult:
    """aliases maps a normalized alias -> normalized canonical name (either
    direction can be the target; checked both ways). Exact/alias/high-confidence
    matches are trusted and used directly; the ambiguous band is flagged for a
    human to confirm or alias rather than guessed at, per SKILL.md's own "flag
    ambiguous mappings" instruction.

    name_extractor defaults to this module's Go-Live-board title parsing;
    pass a different one for boards with a different naming convention (e.g.
    app.tasks.retention_check's extractor for the Retention pipeline's very
    different bracket vocabulary and dollar-amount-in-title style)."""
    from difflib import SequenceMatcher

    aliases = aliases or {}
    target_norm = normalize(target_name)

    best_card_id: str | None = None
    best_card_name: str | None = None
    best_score = 0.0

    for card in cards:
        candidate_norm = normalize(name_extractor(card["name"]))
        if not candidate_norm:
            continue

        if candidate_norm == target_norm:
            return MatchResult(card_id=card["id"], card_name=card["name"], confidence="exact", score=1.0)

        if aliases.get(target_norm) == candidate_norm or aliases.get(candidate_norm) == target_norm:
            return MatchResult(card_id=card["id"], card_name=card["name"], confidence="alias", score=1.0)

        score = SequenceMatcher(None, target_norm, candidate_norm).ratio()
        if score > best_score:
            best_score, best_card_id, best_card_name = score, card["id"], card["name"]

    if best_score >= high_confidence_threshold:
        return MatchResult(card_id=best_card_id, card_name=best_card_name, confidence="high", score=best_score)
    if best_score >= ambiguous_threshold:
        return MatchResult(card_id=best_card_id, card_name=best_card_name, confidence="ambiguous", score=best_score)
    return MatchResult(card_id=None, card_name=None, confidence="none", score=best_score)
