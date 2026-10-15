"""
The sales audit's blend (2026-10-15) -- a port of the one-off prototype's
blend2.py + the dedupe/merge step that lived in its report.py, restructured
as pure functions so the rules in the handoff BRIEF §3-5 are unit-testable
and re-runnable idempotently every day.

Pure on purpose: nothing here does I/O. Everything a rule needs from the
outside world (GHL call history for a contact, a GHL contact lookup for an
Atlas account with no matching opp, Zoom transcript hits) arrives through the
`Lookups` callables, so tests pass fakes and the production wiring
(sales_audit.py) passes real, lazily-evaluated ones -- lazy matters because
call history is only fetched for contacts that actually end up as deals,
not every won opp GHL has ever seen.

"Closed deal" is some COMBINATION of an Atlas account created, a first
charge on the billing sheet, and a Closed/Won GHL opp -- no single source is
complete, so the universe is the union (BRIEF §3.1), and Atlas is an anchor,
not just billing/GHL (the October deals were missed until it became one).

Output is ONE flat list of rows with type "Package" | "Digital Diagnosis".
They share a list for the front end's sake but are never mixed in any total
or table -- the dashboard filters on `type` first (BRIEF §4).
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.tasks import sales_audit_config as cfg

PACKAGE = "Package"
DIGITAL_DIAGNOSIS = "Digital Diagnosis"


@dataclass
class Lookups:
    """I/O the blend needs, injected. All three must be safe to call for any
    input and return an "empty" result rather than raise on a miss."""
    # contact id -> {"contact": {...attribution fields...}, "calls": [ghl call messages]}
    contact_calls: Callable[[str], dict]
    # Atlas account dict -> same shape plus tags/source/dateAdded, or None if
    # no GHL contact could be found for it (the old extra_contacts.py pass).
    find_contact: Callable[[dict], dict | None]
    # (name tokens, close date or None) -> [{"rep", "date", "min", "topic", "via"}]
    zoom_hits: Callable[[list[str], str | None], list[dict]]


# -------------------------------------------------------------------- roster

def tidy_name(name: str | None) -> str:
    """Atlas stores names as typed ("Chris paniagua"). Capitalise any all-lowercase word, leave the rest alone
    (so "McDonald" and "DeAngelo" survive)."""
    return " ".join(w.capitalize() if w.islower() else w for w in (name or "").split())


def _name_key(name: str | None) -> str:
    return " ".join((name or "").lower().split())


@dataclass
class Roster:
    """Who counts as a sales rep -- exactly Atlas's GET /api/users/sales-reps (Bob, 2026-10-15: "the endpoint is the
    level of truth"; no exclusion list). reps = [{name, email}]. ghl_user_reps maps a GHL user id to the roster rep
    it is (matched by email, then name), so a GHL assignee / call is credited to a rep, or to nobody."""
    reps: list[dict]
    ghl_user_reps: dict[str, str] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return [r["name"] for r in self.reps]

    def resolve(self, *, email: str | None = None, name: str | None = None) -> str | None:
        """The roster rep (canonical name) matching an email, else a name; None if nobody on the roster."""
        em = (email or "").strip().lower()
        if em:
            for r in self.reps:
                if r["email"] == em:
                    return r["name"]
        key = _name_key(name)
        if key:
            for r in self.reps:
                if _name_key(r["name"]) == key:
                    return r["name"]
        return None

    def email_for(self, name: str) -> str | None:
        return next((r["email"] for r in self.reps if r["name"] == name), None)

    @classmethod
    def from_atlas(cls, atlas_reps: list[dict], ghl_users: list[dict] | None = None) -> "Roster":
        seen, reps = set(), []
        for u in atlas_reps:
            email = (u.get("email") or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            reps.append({"name": tidy_name(u.get("name")) or email, "email": email})
        roster = cls(reps)
        for g in ghl_users or []:
            rep = roster.resolve(email=g.get("email"), name=g.get("name"))
            if rep:
                roster.ghl_user_reps[g["id"]] = rep
        return roster


# --------------------------------------------------------------- normalizing

def norm(s: str | None) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(inc|llc|co|corp|company|services?|the|and|by|&)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def nkey(s: str | None) -> str:
    return norm(s).replace(" ", "")


def phone10(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")[-10:]


def month_start(s: str) -> dt.datetime:
    """'October 2026' (billing sheet header) -> datetime."""
    return dt.datetime.strptime(s, "%B %Y")


def _day(s: str | None) -> str | None:
    return s[:10] if s else None


def _plus_days(day: str, n: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=n)).isoformat()


def _token_set(x: str | None) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (x or "").lower()) if len(t) > 1}


def same_name(a: str | None, b: str | None) -> bool:
    """Token-subset equality, used to dedupe DD rows by contact name (the same
    person shows up under several pipelines/stages) -- needs >=2 shared tokens
    when one is a strict subset, so "Mike" alone never swallows "Mike Jones"."""
    ta, tb = _token_set(a), _token_set(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    small, large = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(small) >= 2 and small <= large


# ------------------------------------------------------------ source / event

def event_info(tags: list[str] | None, src: str | None, close: str | None) -> tuple[str | None, list[str]]:
    """(event name, matching raw tags). Returns (None, hits) when the contact
    has event tags but EVERY named event post-dates the close (retro-tagging,
    BRIEF §5) -- caller then reclassifies without them. 'Event (unspecified)'
    when the tags say event but name none we know."""
    pool = [x.lower() for x in (tags or [])] + ([src.lower()] if src else [])
    hits = [x for x in pool if re.search(cfg.EVENT_TAG_REGEX, x)]
    named: dict[str, list[str]] = {}
    for x in hits:
        name = next((n for frag, n in cfg.EVENT_MAP if frag in x), x)
        named.setdefault(name, []).append(x)
    ok = []
    for name in named:
        date = cfg.EVENT_DATES.get(name)
        if close is None or date is None or date <= close:
            ok.append((date or "0000", name))
    if not hits:
        return "Event (unspecified)", hits
    if not ok:
        return None, hits
    return max(ok)[1], hits  # most recent qualifying event wins


def classify_source(tags: list[str] | None, src: str | None, attr: dict | None) -> tuple[str, str | None]:
    """Priority Event -> Referral -> Paid Ads -> Organic -> Inbound call ->
    Unknown (BRIEF §3.7), from GHL contact tags / source / attribution."""
    t = " | ".join(tags or []).lower()
    s = (src or "").lower()
    a = json.dumps(attr or {}).lower()
    ev = [x for x in (tags or []) if re.search(r"event|dispatch|attendee|rosewood|workshop|war room|raw\b|nuve (tour|ga|vip)", x.lower())]
    if ev or "event" in s or "dispatch" in s or "rosewood" in s:
        return "Event", ", ".join(ev[:3]) or src
    if "referral" in t or "referral" in s or "affiliate" in t:
        return "Referral", next((x for x in (tags or []) if "referral" in x.lower()), src)
    if (any(x in t for x in ("meta", "facebook", "ads offer", "google_ads", "lsa", "ryno", "scorpion", "video offer", "tiktok"))
            or "paid" in a or s in ("facebook", "google")):
        return "Paid Ads", (attr or {}).get("campaign") or src or "meta tag"
    if "organic" in t or "seo" in t or "website" in s or "form" in s or "meet with" in s:
        return "Organic/Website", src
    if "inbound call" in t:
        return "Inbound call (untagged)", "inbound call, no source tag"
    return "Unknown", src


def resolve_source(tags: list[str], src: str | None, attr: dict | None, close: str | None) -> dict:
    """classify_source + the date-gated event rule, as one step shared by the
    deal path and the DD path (the prototype had this duplicated twice)."""
    source_type, detail = classify_source(tags, src, attr)
    out: dict[str, Any] = {"source_type": source_type, "source_detail": detail, "event": None}
    if source_type != "Event":
        return out
    event, hits = event_info(tags, src, close)
    if event:
        out["event"] = event
        return out
    # Every event tag post-dates the close: retro-tagged. Drop them and fall
    # to the next source tier; never leave it classified as Event.
    rest = [t for t in tags if t.lower() not in hits]
    source_type, detail = classify_source(rest, None, attr)
    out["source_type"] = "Unknown" if source_type == "Event" else source_type
    out["source_detail"] = f"{detail} (event tags {', '.join(hits[:2])} post-date close, ignored)"
    return out


# ------------------------------------------------------------------ indexes

def _notes_amount(atlas: dict) -> float | None:
    m = re.search(r"\$\s?([\d,]{3,7})", atlas.get("salesNotes") or "")
    return float(m.group(1).replace(",", "")) if m else None


def first_charges(billing: list[dict]) -> dict[str, dict]:
    """normalized-name -> earliest charge. First month with a charge is the
    new-client signal (BRIEF §2). Amounts above the plausibility cap were
    already dropped by the reader; defended again here."""
    first: dict[str, dict] = {}
    for row in billing:
        name = row["account"]
        n = norm(name)
        months = {m: v for m, v in (row.get("months") or {}).items()
                  if v is not None and 0 < v <= cfg.MAX_PLAUSIBLE_MONTHLY_CHARGE}
        if not months or name.lower() in cfg.EXCLUDED_BILLING_ACCOUNTS or n in cfg.EXCLUDED_BILLING_ACCOUNTS:
            continue
        key = n.replace(" ", "")
        m = min(months, key=month_start)
        if key not in first or month_start(m) < month_start(first[key]["first_month"]):
            first[key] = {"billing_name": name, "first_month": m, "first_amt": months[m],
                          "retainer": row.get("retainer")}
    return first


class _GhlIndex:
    def __init__(self, opps: list[dict]):
        self.opps = opps
        self.by_contact_key: dict[str, list[dict]] = defaultdict(list)
        for o in opps:
            c = o.get("contact") or {}
            for k in ("e:" + (c.get("email") or "").lower(), "p:" + phone10(c.get("phone"))):
                if len(k) > 3:
                    self.by_contact_key[k].append(o)

    def find(self, atlas: dict | None, billing_name: str) -> dict | None:
        """Prefer id-ish joins (client email/phone from Atlas users), fall back
        to fuzzy name over WON opps only. ADV Master only. Won before open."""
        cand: list[dict] = []
        if atlas:
            for u in atlas.get("users") or []:
                cand += self.by_contact_key.get("e:" + (u.get("email") or "").lower(), [])
                cand += self.by_contact_key.get("p:" + phone10(u.get("phone")), [])
            cand += self.by_contact_key.get("p:" + phone10(atlas.get("phone")), [])
        if not cand:
            nm = norm(billing_name)
            for o in self.opps:
                if o.get("status") != "won":
                    continue
                on = norm((o.get("name") or "") + " " + ((o.get("contact") or {}).get("companyName") or ""))
                if nm and len(nm) >= 5 and (nm in on or on in nm
                                            or difflib.SequenceMatcher(None, nm, norm(o.get("name"))).ratio() > 0.8):
                    cand.append(o)
        cand = [o for o in cand if o.get("pipelineId") == cfg.GHL_ADV_MASTER_PIPELINE]
        cand.sort(key=lambda o: (o.get("status") != "won",))
        return cand[0] if cand else None


# --------------------------------------------------------------------- deals

def _attribute_owner(atlas_rep: dict | None, ghl_rep: str | None, call_score: Counter, roster: Roster) -> tuple[str, str]:
    """Priority: Atlas salesRep IF on the roster -> GHL assignee IF on the roster -> most call minutes before close
    -> Needs rep (BRIEF §3.6). Anyone not on Atlas's sales roster is never credited, wherever the name comes from.
    (A human override is applied after all of this, at render time.)"""
    if atlas_rep:
        name = roster.resolve(email=atlas_rep.get("email"), name=atlas_rep.get("name"))
        if name:
            return name, "Atlas salesRep"
    if ghl_rep:
        return ghl_rep, "GHL opp assignee"
    if call_score:
        return call_score.most_common(1)[0][0], "call activity (Zoom/GHL)"
    return cfg.NEEDS_REP, "none"


def _call_evidence(calls: list[dict], zoom: list[dict], close: str | None, roster: Roster) -> Counter:
    """Score per rep = call minutes + call count. GHL calls only up to close+3
    days (a call long after the close isn't what sold it); Zoom is already
    gated to close+10 by the lookup. No date gate when there's no close date.
    Only roster reps are scored."""
    mins: Counter = Counter()
    for c in calls:
        rep = roster.ghl_user_reps.get(c.get("userId"))
        if not rep:
            continue
        if close and (c.get("dateAdded") or "")[:10] > _plus_days(close, 3):
            continue
        dur = (((c.get("meta") or {}).get("call") or {}).get("duration") or 0) // 60
        mins[rep] += dur + 1
    for h in zoom:
        if h.get("rep"):
            mins[h["rep"]] += (h.get("min") or 0) + 1
    return mins


def la_day(ts: str | None) -> str | None:
    """Atlas stores UTC instants; a sale logged at 6pm Pacific is already
    'tomorrow' in UTC. Close dates are Pacific calendar days."""
    if not ts:
        return None
    try:
        when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:10]
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(ZoneInfo(cfg.ATLAS_TIMEZONE)).date().isoformat()


def referral_partner(atlas: dict | None) -> tuple[str | None, str | None]:
    """(name, partnerId) from Atlas's `referralPartner` -- `{name, partnerId}`
    or null (live as of 2026-10-15; null on every pre-existing account, since
    partners are only set going forward, so the GHL-tag fallback in
    classify_source still carries older deals). partnerId is the stable
    grouping key and can itself be null. A bare name string is tolerated."""
    rp = (atlas or {}).get("referralPartner")
    if isinstance(rp, dict):
        name, pid = rp.get("name"), rp.get("partnerId")
    else:
        name, pid = rp, None
    name = name.strip() if isinstance(name, str) else None
    return (name or None), (str(pid) if pid else None)


def atlas_initial_value(atlas: dict) -> tuple[float | None, float | None]:
    """(initial monthly value, base amount) from Atlas's billing object.
    Initial = baseAmount less the $1,000 intro discount when introPricing is
    on. None when Atlas has no usable base amount (blank, or 0 as on
    prepaid accounts, where base is $0 until the prepay lapses)."""
    billing = atlas.get("billing") or {}
    base = billing.get("baseAmount")
    if not base or base <= 0:
        return None, None
    initial = base - cfg.INTRO_DISCOUNT if billing.get("introPricing") else base
    return max(initial, 0), base


def _atlas_era(a: dict) -> bool:
    return (not a.get("legacy")) and (la_day(a.get("createdAt")) or "") >= cfg.ATLAS_ERA_START \
        and a["companyName"].lower() not in cfg.EXCLUDED_ATLAS_ACCOUNTS


def build_deals(data: dict, lk: Lookups) -> list[dict]:
    """Package deals (plus any billing-only deal that prices as Digital
    Diagnosis). Two paths:

    1. ATLAS ERA (created >= Jul 1, non-legacy): every Atlas account is a
       sold Package -- no inference. Atlas is the source of truth for close
       date and rep; Atlas billing for initial value, with the billing
       sheet's first charge only a spot check.
    2. PRE-ATLAS / NOT IN ATLAS: billing-sheet accounts first charged on/
       after Jun 1 that are not an Atlas-era account (June deals, which
       predate Atlas, and any real sale that never made it into Atlas --
       flagged not_in_atlas). Close date from GHL won date else billing
       month; Atlas createdAt deliberately ignored (backdated imports).

    `data` keys: billing, atlas, master_opps, ghl_users, roster (a Roster)."""
    start = dt.datetime.fromisoformat(cfg.AUDIT_START)
    atlas = data["atlas"]
    atlas_by_key = {nkey(a["companyName"]): a for a in atlas}

    def find_atlas(name: str) -> dict | None:
        k = nkey(name)
        if k in atlas_by_key:
            return atlas_by_key[k]
        best = difflib.get_close_matches(k, list(atlas_by_key), 1, 0.8)
        return atlas_by_key[best[0]] if best else None

    ghl = _GhlIndex(data["master_opps"])
    first = first_charges(data["billing"])
    era = [a for a in atlas if _atlas_era(a)]
    era_ids = {a["id"] for a in era}

    deals = []
    # Billing rows already explained by an Atlas-era account (matched by
    # name, same matcher as always) so path 2 doesn't double count them.
    billed_for_era: dict[str, dict] = {}
    claimed_billing: set[str] = set()
    for a in era:
        k = nkey(a["companyName"])
        match = first.get(k)
        if match is None:
            best = difflib.get_close_matches(k, [x for x in first if x not in claimed_billing], 1, 0.8)
            match = first.get(best[0]) if best else None
            k = best[0] if best else None
        if match is not None and k:
            claimed_billing.add(k)
            billed_for_era[a["id"]] = match
    for a in era:
        deals.append(_one_deal(a, billed_for_era.get(a["id"]), ghl.find(a, a["companyName"]), data, lk, atlas_era=True))

    for key, v in first.items():
        if key in claimed_billing or month_start(v["first_month"]) < start:
            continue
        a = find_atlas(v["billing_name"])
        if a and a["id"] in era_ids:
            continue
        deals.append(_one_deal(a, v, ghl.find(a, v["billing_name"]), data, lk, atlas_era=False))
    return deals


def _one_deal(a: dict | None, bill: dict | None, o: dict | None, data: dict, lk: Lookups, *, atlas_era: bool) -> dict:
    """One row. `a` Atlas account (None if not in Atlas), `bill` that client's
    first billing-sheet charge (None if not on the sheet yet), `o` the ADV
    Master opp, if any."""
    ghl_won = _day(o.get("lastStatusChangeAt")) if o else None
    backfill = bool(o and (ghl_won == cfg.GHL_BACKFILL_DATE or (ghl_won or "") < cfg.AUDIT_START))
    roster: Roster = data["roster"]
    ghl_assigned = (data.get("ghl_users") or {}).get(o.get("assignedTo")) if o else None
    ghl_rep = roster.ghl_user_reps.get(o.get("assignedTo")) if o else None
    contact_name = (o["contact"]["name"] if o and o.get("contact") else None) or \
        (a["users"][0]["name"] if a and a.get("users") else None)
    client = a["companyName"] if a else bill["billing_name"]
    sheet_amt = bill.get("first_amt") if bill else None

    # ---- GHL contact attribution + calls: opp-linked contact when there is
    # an opp, else a lookup by the Atlas account's own contacts.
    gc = lk.contact_calls(o["contactId"]) if o and o.get("contactId") else {}
    tags = ((o.get("contact") or {}).get("tags") if o else None) or []
    src = (gc.get("contact") or {}).get("source") if gc else (o.get("source") if o else None)
    attr = (gc.get("contact") or {}).get("attributionSource") if gc else None
    contact_added = None
    if not o and a:
        ex = lk.find_contact(a)
        if ex:
            gc, tags, src, attr = {"calls": ex.get("calls") or [], "contact": ex}, ex.get("tags") or [], ex.get("source"), ex.get("attributionSource")
            contact_added = _day(ex.get("dateAdded"))

    # ---- close date
    if atlas_era:
        close, close_basis = la_day(a["createdAt"]), "atlas created"
    elif ghl_won and not backfill:
        close, close_basis = ghl_won, "ghl won"
    elif bill:
        close, close_basis = None, "billing first charge (month only)"
    else:
        close, close_basis = None, "unknown"
    close_month = close[:7] if close else (month_start(bill["first_month"]).strftime("%Y-%m") if bill else None)

    # ---- amount: Atlas billing is the source of truth, sheet the spot check
    initial, base = atlas_initial_value(a) if a else (None, None)
    if atlas_era and initial is not None:
        amount, amount_basis = initial, "Atlas billing"
    elif sheet_amt:
        amount, amount_basis = sheet_amt, "billing sheet"
    else:
        est = _notes_amount(a) if a else None
        amount, amount_basis = (est, "Atlas notes (est.)") if est else (None, "none yet")

    # ---- source: an explicit Atlas referral partner outranks inferred tags
    partner, partner_id = referral_partner(a)
    if partner:
        src_info = {"source_type": "Referral", "source_detail": f"Atlas referral partner: {partner}", "event": None}
    elif o or gc.get("contact"):
        date_for_gate = close or (month_start(bill["first_month"]).strftime("%Y-%m-%d") if bill else None)
        src_info = resolve_source(tags, src, attr, date_for_gate)
    else:
        src_info = {"source_type": "Unknown", "source_detail": "no GHL match", "event": None}

    # ---- rep
    names = [contact_name, client] + ([u.get("name") for u in a.get("users") or []] if a else [])
    zoom = lk.zoom_hits([n for n in names if n], close)
    score = _call_evidence(gc.get("calls") or [], zoom, close, roster)
    owner, basis = _attribute_owner((a or {}).get("salesRep"), ghl_rep, score, roster)

    # ---- type: Atlas-era is ALWAYS a package; elsewhere price decides
    if atlas_era:
        is_dd = False
    else:
        ghl_value = o.get("monetaryValue") if o else None
        is_dd = (amount or 0) in cfg.DD_PRICES or (ghl_value or 0) in cfg.DD_PRICES

    # ---- flags (drive the dashboard's "needs a look" filters)
    flags = []
    if owner == cfg.NEEDS_REP:
        flags.append("needs_rep")
    if not amount:
        flags.append("no_amount")
    if not o and not gc.get("contact"):
        flags.append("no_ghl_match")
    if a and a.get("stage") == "closed":
        flags.append("atlas_closed")
    if not atlas_era and not a:
        flags.append("not_in_atlas")
    if not atlas_era and ghl_won is None and not close:
        flags.append("estimated_date")
    if score and owner != cfg.NEEDS_REP and score.most_common(1)[0][0] != owner:
        flags.append("owner_conflict")
    if atlas_era and initial is not None and sheet_amt and abs(initial - sheet_amt) > cfg.AMOUNT_TOLERANCE:
        flags.append("amount_mismatch")
    onboarding = (a or {}).get("onboarding") or {}
    if atlas_era and onboarding.get("total") and onboarding.get("completed", 0) < onboarding["total"]:
        flags.append("onboarding_incomplete")

    return {
        "key": f"atlas:{a['id']}" if a else (f"ghl:{o['id']}" if o else f"bill:{nkey(client)}"),
        "type": DIGITAL_DIAGNOSIS if is_dd else PACKAGE,
        "client": client, "contact": contact_name,
        "close_date": close, "close_month": close_month, "close_date_basis": close_basis,
        "amount": amount, "amount_basis": amount_basis,
        "base_amount": base, "sheet_first_charge": sheet_amt,
        "retainer": (bill or {}).get("retainer"),
        "owner": owner, "owner_basis": basis, "call_score": dict(score),
        "noncore_assignee": ghl_assigned if ghl_assigned and not ghl_rep else None,
        **src_info, "referral_partner": partner, "referral_partner_id": partner_id, "tags": tags[:8], "where": None,
        "atlas_era": atlas_era, "atlas_id": a["id"] if a else None,
        "atlas_stage": a.get("stage") if a else None, "atlas_created": la_day(a["createdAt"]) if a else None,
        "onboarding": onboarding or None,
        "ghl_opp": o["id"] if o else None, "ghl_status": o.get("status") if o else None,
        "ghl_won_date": ghl_won, "ghl_assigned": ghl_assigned, "ghl_contact_added": contact_added,
        "zoom_calls": len(zoom), "zoom_detail": zoom[:6], "flags": flags,
    }


# ------------------------------------------------------- Digital Diagnosis rows

def build_dd_rows(data: dict) -> list[dict]:
    """Stage-based DD closes from all three pipelines (BRIEF §3.5): a stage
    named 'Digital Diagnosis' that starts closed/sold/purchased, OR a won opp
    priced at a DD price point. Deduped by contact-name tokens within 14 days
    (the same person moves through several pipelines)."""
    stages = data["stages"]
    users = data.get("ghl_users") or {}
    roster: Roster = data["roster"]
    rows = []
    for o in data["master_opps"] + data["other_opps"]:
        pipeline, stage = stages.get(o.get("pipelineStageId"), ("?", "?"))
        sn = stage.lower()
        by_stage = ("digital diagnosis" in sn and o.get("status") in ("won", "open")
                    and (o.get("lastStageChangeAt") or "") >= cfg.AUDIT_START
                    and sn.startswith(("closed", "sold", "purchased")))
        by_price = (o.get("status") == "won" and o.get("monetaryValue") in cfg.DD_PRICES
                    and (o.get("lastStatusChangeAt") or "") >= cfg.AUDIT_START)
        if not (by_stage or by_price):
            continue
        tags = (o.get("contact") or {}).get("tags") or []
        date = _day(o["lastStageChangeAt"])
        assigned = users.get(o.get("assignedTo"))
        ghl_rep = roster.ghl_user_reps.get(o.get("assignedTo"))
        owner = ghl_rep or cfg.NEEDS_REP
        rows.append({
            "key": f"dd:{o['id']}", "type": DIGITAL_DIAGNOSIS,
            "client": o.get("name"), "contact": o.get("name"),
            "close_date": date, "close_month": date[:7], "close_date_basis": "ghl stage change",
            "amount": o.get("monetaryValue"), "amount_basis": "ghl value" if o.get("monetaryValue") else "none yet",
            "retainer": None, "base_amount": None, "sheet_first_charge": None, "referral_partner": None, "referral_partner_id": None,
            "atlas_era": False, "onboarding": None, "owner": owner,
            "owner_basis": "GHL opp assignee" if owner != cfg.NEEDS_REP else "none", "call_score": {},
            "noncore_assignee": assigned if assigned and not ghl_rep else None,
            **resolve_source(tags, o.get("source"), None, date), "tags": tags[:8],
            "where": f"{pipeline} · {stage}",
            "atlas_id": None, "atlas_stage": None, "atlas_created": None,
            "ghl_opp": o["id"], "ghl_status": o.get("status"), "ghl_won_date": date,
            "ghl_assigned": assigned, "ghl_contact_added": None, "zoom_calls": 0, "zoom_detail": [],
            "flags": ["needs_rep"] if owner == cfg.NEEDS_REP else [],
        })
    uniq: list[dict] = []
    for r in sorted(rows, key=lambda r: r["close_date"]):
        dup = any(same_name(r["client"], u["client"])
                  and abs((dt.date.fromisoformat(r["close_date"]) - dt.date.fromisoformat(u["close_date"])).days) <= 14
                  for u in uniq)
        if not dup:
            uniq.append(r)
    return uniq


def merge_sales(deals: list[dict], dd_rows: list[dict]) -> list[dict]:
    """Deals that price as Digital Diagnosis but weren't caught by the stage
    scan join the DD list. Package rows pass through. Final pass dedupes by
    key and by (type, normalized client) -- the prototype listed OG Plumbing
    twice because two billing names mapped to one Atlas account."""
    out = [d for d in deals if d["type"] == PACKAGE]
    dd = list(dd_rows)
    for d in deals:
        if d["type"] != DIGITAL_DIAGNOSIS:
            continue
        if not any(same_name(d["contact"], r["client"]) or same_name(d["client"], r["client"]) for r in dd):
            dd.append({**d, "where": f"Billing / Atlas ({d['client']})"})
    seen: set = set()
    rows = []
    for r in out + dd:
        ident = (r["type"], r["key"] if r["type"] == DIGITAL_DIAGNOSIS else nkey(r["client"]))
        if ident in seen or r["key"] in seen:
            continue
        seen.add(ident)
        seen.add(r["key"])
        rows.append(r)
    rows.sort(key=lambda r: (r["close_date"] or (r["close_month"] or "") + "-99"), reverse=True)
    return rows


def build_sales_rows(data: dict, lk: Lookups) -> list[dict]:
    return merge_sales(build_deals(data, lk), build_dd_rows(data))


# ---------------------------------------------------------------- zoom helper

_VTT_TIME = re.compile(r"(\d+):(\d\d):(\d\d)\.\d+\s*-->\s*(\d+):(\d\d):(\d\d)\.\d+")


def vtt_minutes(vtt: str | None) -> int:
    """Call length from a transcript's last cue end time. ZoomCallRecord keeps
    the full VTT but not the meeting duration, and duration is what ranks the
    reps' involvement -- so recover it from the text."""
    last = None
    for m in _VTT_TIME.finditer(vtt or ""):
        last = m
    if not last:
        return 0
    h, mi, s = int(last.group(4)), int(last.group(5)), int(last.group(6))
    return (h * 3600 + mi * 60 + s) // 60
