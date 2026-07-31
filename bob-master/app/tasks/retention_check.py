"""
Cancel-intent cross-check against the Retention & Cancel/Save Pipeline —
ACCURACY RULES §2 in SKILL.md: "board LIVE is not proof of active... before
listing any client as live/went-live, check the Retention pipeline." Real
sandbox board data confirms the exact status vocabulary BOB-OPERATIONS.md
describes: new requests, save attempt (48hours), happy holding area, free
month active (all-hands), saved +, churned x, complete.

Two things the real board surfaced that need explicit handling:
  - Administrative/template cards mixed in with real clients — "📋 TEMPLATE —
    copy this for every request", "🔧 KYLE — Set up & own the Retention
    pipeline management view", and an explicit "[EXAMPLE] CANCEL — Acme
    Plumbing — $2,500" whose own description says to delete it once a real
    request exists. These must never be matched against a real client —
    is_administrative_card() filters them before matching is even attempted.
  - Title convention is different (and messier) than the Go-Live board's:
    dollar amounts and reasons embedded directly in the title
    ("[SAVE] Amaral's Construction — $3,500 (ghosting)"), inconsistent
    bracket vocabulary, and at least one real title with metadata BEFORE the
    client name ("📞 WIN-BACK — WestCoast Fence Pros") that this extractor
    gets wrong (extracts "WIN-BACK", not the client name) — a known,
    accepted gap: it fails safely (no match) rather than matching wrong.
"""
from __future__ import annotations

import re

# Definitely cancelled — treat as CANCEL per ACCURACY RULES §2, regardless of
# what the board status/heartbeat spend otherwise suggests.
CHURNED_STATUSES = {"churned x"}
# An active cancel/save event is in progress — board-says-live shouldn't be
# reported without surfacing this.
ACTIVE_RISK_STATUSES = {"new requests", "save attempt (48hours)", "save attempt (48 hrs)"}
# Resolved positively — client is staying, in some form. Not a cancel signal.
RESOLVED_POSITIVE_STATUSES = {
    "happy holding area",
    "saved +",
    "saved+",
    "free month active (all-hands)",
    "free month active",
}

_ADMIN_CARD_MARKERS = ("[EXAMPLE]", "📋 TEMPLATE", "🔧 KYLE", "🔧 SET UP")

_LEADING_BRACKET_RE = re.compile(r"^\[[^\]]*\]\s*", re.UNICODE)
_LEADING_SYMBOLS_RE = re.compile(r"^[^\w]+", re.UNICODE)
_TRAILING_DOLLAR_RE = re.compile(r"\s*\$[\d,]+.*$", re.UNICODE)
_TRAILING_DASH_RE = re.compile(r"\s+[-–—]\s+.*$", re.UNICODE)
_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$", re.UNICODE)
_TRAILING_DASH_CHARS_RE = re.compile(r"[-–—\s]+$", re.UNICODE)


def is_administrative_card(card_title: str) -> bool:
    """Template/setup/demo cards — must never be treated as a real client."""
    upper = card_title.upper()
    return any(marker.upper() in upper for marker in _ADMIN_CARD_MARKERS)


def extract_retention_candidate_name(card_title: str) -> str:
    """Best-effort client-name extraction for Retention board titles. See
    module docstring for the known "metadata before the name" gap."""
    name = card_title.strip()
    name = _LEADING_BRACKET_RE.sub("", name)
    name = _LEADING_SYMBOLS_RE.sub("", name)
    name = _TRAILING_DOLLAR_RE.sub("", name)
    name = _TRAILING_DASH_RE.sub("", name)
    name = _TRAILING_PAREN_RE.sub("", name)
    name = _TRAILING_DASH_CHARS_RE.sub("", name)
    return name.strip()
