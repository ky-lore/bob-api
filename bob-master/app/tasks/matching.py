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
    confidence: str  # "exact" | "alias" | "ambiguous" | "none"
    score: float


def find_best_match(
    target_name: str,
    cards: list[dict],  # each: {"id": str, "name": str}
    *,
    aliases: dict[str, str] | None = None,
    ambiguous_threshold: float = 0.72,
) -> MatchResult:
    """aliases maps a normalized alias -> normalized canonical name (either
    direction can be the target; checked both ways). Exact/alias matches are
    trusted; anything else above the threshold is flagged ambiguous rather
    than silently accepted, per SKILL.md's own "flag ambiguous mappings"
    instruction — this is not a place to guess confidently."""
    from difflib import SequenceMatcher

    aliases = aliases or {}
    target_norm = normalize(target_name)

    best: MatchResult = MatchResult(card_id=None, card_name=None, confidence="none", score=0.0)

    for card in cards:
        candidate_norm = normalize(extract_candidate_name(card["name"]))
        if not candidate_norm:
            continue

        if candidate_norm == target_norm:
            return MatchResult(card_id=card["id"], card_name=card["name"], confidence="exact", score=1.0)

        if aliases.get(target_norm) == candidate_norm or aliases.get(candidate_norm) == target_norm:
            return MatchResult(card_id=card["id"], card_name=card["name"], confidence="alias", score=1.0)

        score = SequenceMatcher(None, target_norm, candidate_norm).ratio()
        if score > best.score:
            best = MatchResult(card_id=card["id"], card_name=card["name"], confidence="ambiguous", score=score)

    if best.score >= ambiguous_threshold:
        return best
    return MatchResult(card_id=None, card_name=None, confidence="none", score=best.score)
