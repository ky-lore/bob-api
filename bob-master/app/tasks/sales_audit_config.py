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

# Atlas billing.introPricing = "on the first-two-months rate ($1,000 off any
# tier)" (Client.js). The initial monthly charge is baseAmount minus this.
INTRO_DISCOUNT = 1000
# Atlas initial value vs the billing sheet's first charge: differences up to
# this are rounding noise; beyond it the row is flagged amount_mismatch.
# Informational only -- the sheet's first charge can legitimately be prorated.
AMOUNT_TOLERANCE = 1.0

# GHL user id -> canonical rep name. Reps who count (BRIEF §4, decided by
# Bob -- don't re-litigate). Simon Ting, Allondra Guitierrez and Makayla
# Hudgens must NEVER be credited with a deal, even when GHL assigns them the
# opp: their assignment is ignored and the deal falls back to call evidence,
# else "Needs rep" (a human assigns it via an override).
REPS_BY_GHL_USER = {
    "VcAw8YaXQU5UKKrQqmc7": "Paul Rastrelli",
    "zFFksf6pFR54PxcMNgUI": "Kyle Kellner",
    "oaxfQ14pFppgbEY1QtUs": "Jaden Bashaw",
    "QdxKPdYt22ffj9Ue1OO5": "Chris Paniagua",
}
REP_NAMES = list(REPS_BY_GHL_USER.values())
NEEDS_REP = "Needs rep"

# Zoom host email (as stored in ZoomCallRecord.host_email) -> rep.
REPS_BY_ZOOM_EMAIL = {
    "paul@advancedmarketers.co": "Paul Rastrelli",
    "kylekellner@advancedmarketers.co": "Kyle Kellner",
    "jbashaw@advancedmarketers.co": "Jaden Bashaw",
    "chris@advancedmarketers.co": "Chris Paniagua",
}

# GHL pipelines. Only ADV Master carries "real client" deals; the other two
# are scanned only for stage-based Digital Diagnosis closes.
GHL_ADV_MASTER_PIPELINE = "1rySFshGqxtuO5hF2z2f"
GHL_DD_PIPELINES = ("EN99INlGiYdView6PvaR", "6hhMLoGLThgmOAr4bTVG")  # RAW Houston, Dispatch 01

# A GHL won date of exactly this day is a bulk backfill (~35 deals) -- not a
# real close date, ignored (BRIEF §3.3).
GHL_BACKFILL_DATE = "2026-06-01"

# Digital Diagnosis is the ~$499/$500 entry product.
DD_PRICES = (499, 499.99, 500)

# Billing-sheet noise that is not a real client (BRIEF §4). Compared against
# both the raw account name (lowercased) and its normalized form.
EXCLUDED_BILLING_ACCOUNTS = {
    "declined", "charged", "dd", "dispatch", "digital diagnosis", "one off charges",
    "billing testing 2", "mclaren racing", "palm valley insurance sponsor", "embroidery",
    "enbroidery", "el barber", "luis barber barbershop", "jimmy service pros",
    "first rate restoration",
}
# Atlas-only accounts that are test/sponsor records, never a sale.
EXCLUDED_ATLAS_ACCOUNTS = {
    "one off charges", "digital diagnosis", "billing testing 2", "red bull racing", "mclaren racing",
}

# Any single monthly charge above this is treated as a bad cell, not revenue.
# Added after a 16-digit card-number-shaped value turned up in the prototype's
# billing snapshot's month columns -- dropped (and logged by the reader)
# rather than summed into first-charge revenue.
MAX_PLAUSIBLE_MONTHLY_CHARGE = 50_000

# Billing sheet (BRIEF §2). The sheet ALSO holds full card numbers/CVVs in
# columns C-G -- the reader (sales_audit_sources.read_billing) is allowlist
# based on header, never a blanket A:Z read, so those can never be fetched.
BILLING_SHEET_ID = "1hHDkosxPJWghyxa_ovjDray3GRmqGX1dlxkee9QEJDU"
BILLING_SHEET_GID = 0
# Header allowlist for the billing sheet reader: a month column ("October 2024")
# or the retainer column (sheet header is spelled "Rentainer"). Anything else
# -- notably the card number/CVV columns -- is never requested.
BILLING_MONTH_HEADER_REGEX = r"^[A-Z][a-z]+ \d{4}$"
BILLING_RETAINER_HEADER_REGEX = r"^r(e|en)tainer$"

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
