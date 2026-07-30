"""
Bridges a matched ClickUp card to the package-clock inputs evaluate_package_clock()
needs: package identification and day count.

PACKAGE IDENTIFICATION, per SKILL.md's priority order:
  1. ClickUp pkg-* tags — checked, but confirmed NOT in use on the real board
     (every card's tags are either "newclientgolivetracker" or empty; see
     docs/TASK-INVENTORY.md / chat history). Kept in case the team adds them.
  2. GHL "Package Type" contact field — SKIPPED. Would need a bridge from
     heartbeat/card account name to a GHL contact ID, which doesn't exist yet.
     Documented gap, not a silent omission.
  3. Legacy bracket markers in the card title — the only thing actually
     working today. Only the four patterns SKILL.md explicitly names; anything
     else (e.g. a card titled "[MKTG — SEO pkg]") falls through to
     "unidentified" rather than guessing at an unlisted combination.
  4. Nothing identifiable — "unidentified", same as SKILL.md's fallback.

DAY COUNT: some cards have a human-written "Day N" in the title — evidence in
real data (e.g. ColdRiite Walk-Ins, "Day 374") that a card's own date_created
in ClickUp can be much newer than when the client actually signed, because
cards get recreated/carried forward. A manual "Day N" is preferred when
present; ClickUp's date_created is only a fallback estimate, not falsified
the way the heartbeat sheet's timezone was.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

_KNOWN_PACKAGE_TAGS = {"pkg-mktg", "pkg-web", "pkg-web-custom", "pkg-seo", "pkg-web-seo", "pkg-free-promo"}

# Only the patterns SKILL.md's PACKAGE IDENTIFICATION section names explicitly.
_LEGACY_MARKER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"web\+video", re.I), "pkg-web-custom"),
    (re.compile(r"free.*promo", re.I), "pkg-free-promo"),
    (re.compile(r"web pkg", re.I), "pkg-web"),
    (re.compile(r"\bmktg\b", re.I), "pkg-mktg"),
]

_DAY_COUNT_RE = re.compile(r"\bday\s+(\d+)\b", re.I)


def identify_package(card_tags: list[str], card_name: str) -> str:
    for tag in card_tags:
        if tag in _KNOWN_PACKAGE_TAGS:
            return tag
    for pattern, package in _LEGACY_MARKER_PATTERNS:
        if pattern.search(card_name):
            return package
    return "unidentified"


def extract_manual_day_count(card_name: str) -> int | None:
    match = _DAY_COUNT_RE.search(card_name)
    return int(match.group(1)) if match else None


def days_since_created(date_created_ms: str) -> int:
    """date_created is a real epoch-millisecond timestamp — unambiguous UTC,
    unlike the heartbeat sheet's naive local timestamps. No timezone guessing
    needed here."""
    created = datetime.fromtimestamp(int(date_created_ms) / 1000, tz=timezone.utc)
    return (datetime.now(timezone.utc) - created).days


def resolve_day_count(card_name: str, date_created_ms: str) -> int:
    manual = extract_manual_day_count(card_name)
    return manual if manual is not None else days_since_created(date_created_ms)
