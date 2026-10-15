"""
Business rules for the sales audit (2026-10-15), lifted out of the one-off
prototype's blend2.py so they're one reviewable place rather than scattered
literals. Deliberately plain module constants for Phase 1 (not a DB config
table yet) -- these change a few times a year, not daily, and a code change
+ push is an acceptable way to edit them until someone actually needs an
in-app editor. See the handoff BRIEF.md §4-5 for where each decision came
from; the "don't re-litigate" ones are marked.
"""
from __future__ import annotations

# First close date the audit covers. Everything earlier is out of scope.
AUDIT_START = "2026-06-01"

# Atlas's real inception (Bob, 2026-10-15: "july 1st this year is probably the
# first date with real clients"). Every Atlas account created on/after this
# date -- Los Angeles calendar day -- is a real, sold PACKAGE, always, never
# Digital Diagnosis. Before it, Atlas createdAt is NOT trustworthy: the
# legacy bulk import (roadmap/server/scripts/importClients.js --legacy)
# backdates createdAt across Jan 15-Jun 30, which is why e.g. Reel Electric
# shows Feb but was first billed in June. Jun 1-Jun 30 deals therefore come
# from billing sheet + GHL only.
ATLAS_ERA_START = "2026-07-01"
ATLAS_TIMEZONE = "America/Los_Angeles"

# REP ROSTER (Bob, 2026-10-15): the roster is whatever Atlas says -- GET /api/users/sales-reps (active users in the
# sales department), pulled on every run and stored in the run payload. "The endpoint is the level of truth":
# no exclusion list, no hardcoded names/IDs. GHL users and Zoom hosts are matched to roster reps by EMAIL (name as a
# fallback for GHL). The four below are ONLY a fallback for when that endpoint can't be reached AND no earlier run
# has a stored roster, and for runs stored before the roster existed -- never the source of truth.
LEGACY_ROSTER = [
    {"name": "Paul Rastrelli", "email": "paul@advancedmarketers.co"},
    {"name": "Kyle Kellner", "email": "kylekellner@advancedmarketers.co"},
    {"name": "Jaden Bashaw", "email": "jbashaw@advancedmarketers.co"},
    {"name": "Chris Paniagua", "email": "chris@advancedmarketers.co"},
]
NEEDS_REP = "Needs rep"

# GHL pipelines. Only ADV Master carries "real client" deals; the other two
# are scanned only for stage-based Digital Diagnosis closes.
GHL_ADV_MASTER_PIPELINE = "1rySFshGqxtuO5hF2z2f"
GHL_DD_PIPELINES = ("EN99INlGiYdView6PvaR", "6hhMLoGLThgmOAr4bTVG")  # RAW Houston, Dispatch 01

# Digital Diagnosis is the ~$499/$500 entry product.
DD_PRICES = (499, 499.99, 500)

# Atlas-only accounts that are test/sponsor records, never a sale.
EXCLUDED_ATLAS_ACCOUNTS = {
    "one off charges", "digital diagnosis", "billing testing 2", "red bull racing", "mclaren racing",
}

# Event tag fragments -> named event, checked IN ORDER (first fragment found
# in the lowercased tag wins), so more specific fragments come first.
EVENT_MAP = [
    ("paid dispatch 01", "Dispatch 01 (Orange County)"), ("purchased may 8", "RAW Houston (May 8)"),
    ("dispatch", "Dispatch 01 (Orange County)"), ("sept 25", "Dispatch 01 (Orange County)"),
    ("sept 20", "Sept 20 War Room (LA)"), ("war room", "Sept 20 War Room (LA)"),
    ("april 17", "Apr 17 Rosewood contractor workshop"), ("april 18", "Apr 17 Rosewood contractor workshop"),
    ("rosewood", "Apr 17 Rosewood contractor workshop"), ("contractor workshop", "Apr 17 Rosewood contractor workshop"),
    ("rw ", "Apr 17 Rosewood contractor workshop"),
    ("raw may 8", "RAW Houston (May 8)"), ("raw", "RAW Houston (May 8)"), ("houston", "RAW Houston (May 8)"),
    ("nuve", "Nuve event"), ("empresarios", "Empresarios event"), ("may 15", "May 15 event"),
    ("solarcon", "SolarCon"), ("avanza", "Avanza event"), ("driven event", "Driven event"),
]
# An event only counts for a deal if its date <= the close date (retro-tagged
# events are ignored -- BRIEF §5). Events with no date here always qualify.
# ASSUMED dates, not yet confirmed with Bob -- see BRIEF §10 Q2. "may 15
# attendee" is also likely applied to everyone INVITED, so May 15 counts are
# an upper bound.
EVENT_DATES = {
    "Dispatch 01 (Orange County)": "2026-09-25", "RAW Houston (May 8)": "2026-05-08",
    "May 15 event": "2026-05-15", "Sept 20 War Room (LA)": "2026-09-20",
    "Apr 17 Rosewood contractor workshop": "2026-04-17", "Empresarios event": "2026-02-28",
}
EVENT_TAG_REGEX = r"event|dispatch|attendee|rosewood|workshop|war room|\braw\b|nuve|empresarios|solarcon|avanza"
