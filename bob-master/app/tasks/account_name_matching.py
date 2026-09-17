"""
Fuzzy name matching for attributing something that only carries a free-text
name (a Zoom meeting topic, a ClickUp folder) to a real Atlas account.

This is a deliberate, narrow exception to this codebase's "zero
fuzzy-matching" stance (see daily_go_live_audit.py's module docstring): that
stance is about not re-deriving Atlas-owned facts (stage, is_live) from a
lower-fidelity system when Atlas already has the answer. This is a different
problem -- Atlas has no Zoom meeting ID or ClickUp folder ID field for us to
join on directly (that's the ClickUp folder ID re-bridging problem, see chat
history, 2026-08-25/2026-09-17), so name-matching is the only way to get
there at all, not a shortcut around a source of truth.

Extracted 2026-09-17 after writing the same normalize/best_match pair a
third time (twice as one-off reconciliation scripts for the ClickUp
folder re-bridge, now for real inside the Zoom call sync).
"""
from __future__ import annotations

import difflib
import re

_SUFFIX_RE = re.compile(r"\b(inc|llc|corp|co|ltd|company)\b\.?")
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
_PAREN_RE = re.compile(r"\([^)]*\)")
# Boilerplate that only ever shows up on the Zoom-topic side of a match (a
# real company name never contains "advanced marketers"/"weekly meeting"),
# so stripping it here is a no-op for Atlas/ClickUp names and harmless to
# apply unconditionally. Real misses found smoke-testing the Zoom call sync,
# 2026-09-17: without this, a short client nickname ("Speedee X Advanced
# Marketers") scored too low against the full company name ("Speedee Drains
# and Plumbing") because the boilerplate diluted the token overlap -- and
# conversely, a topic with NO real client name at all ("Introduction") could
# still land a coincidentally-high character-diff ratio against an unrelated
# company purely because both are short, generic strings.
_BOILERPLATE_RE = re.compile(
    r"\b(advanced marketers|weekly meeting|bi[- ]?weekly( meeting)?|meeting|\bam\b|\bx\b)\b"
)


def normalize(name: str) -> str:
    """Strips emoji, parenthetical asides, punctuation, legal suffixes, and
    Zoom-topic boilerplate; lowercases; collapses whitespace. NOT infallible
    -- see the ClickUp re-bridge's real misses (e.g. "(dba X)" aliases
    getting thrown away along with the parens) before trusting a
    low-confidence result."""
    name = _EMOJI_RE.sub("", name)
    name = _PAREN_RE.sub("", name)
    name = name.lower()
    name = name.replace("&", " and ")
    name = re.sub(r"heating\s+(and|&)\s+air\s+conditioning", "hvac", name)
    name = re.sub(r"[.,'\-_/|]", " ", name)
    name = _BOILERPLATE_RE.sub(" ", name)
    name = _SUFFIX_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _token_jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _containment_score(a: str, b: str) -> float:
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 4 and shorter in longer:
        return 0.9 + 0.1 * (len(shorter) / len(longer))
    return 0.0


def combined_score(a: str, b: str) -> float:
    """a and b must already be normalize()d. Max of three signals: exact
    character-diff ratio, token overlap (catches reordered/partial word
    matches), and substring containment (catches "X Fence" containing "X")."""
    return max(
        difflib.SequenceMatcher(None, a, b).ratio(),
        _token_jaccard(a, b),
        _containment_score(a, b),
    )


def best_match(target_norm: str, candidates_norm: dict[str, str]) -> tuple[str | None, float]:
    """candidates_norm: {original_name: normalize(original_name)}. Returns
    (best_original_name, score), or (None, 0.0) if candidates is empty."""
    best_name, best_score = None, 0.0
    for original, norm in candidates_norm.items():
        if norm == target_norm:
            return original, 1.0
        score = combined_score(target_norm, norm)
        if score > best_score:
            best_name, best_score = original, score
    return best_name, best_score
