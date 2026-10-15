"""
The sales audit's blend (2026-10-15): pure functions, no I/O. Everything a rule needs from outside (GHL call
history for a contact, a GHL contact lookup for an Atlas account with no matching opp, Zoom transcript hits) arrives
through the `Lookups` callables, so tests pass fakes and the production wiring (sales_audit.py) passes real, lazily
evaluated ones.

ATLAS IS THE SOURCE OF TRUTH (Bob, 2026-10-15). A deal is an Atlas account, full stop -- the old billing-sheet join
(which created duplicate "sales" whenever a client's sheet name differed from its Atlas name) is gone:
  - created on/after Jul 1 (Pacific day), not a legacy import  -> a sold Package, closed on createdAt;
  - a LEGACY import (createdAt is a backdated import date)     -> counts only if Atlas has billing.firstChargeDate
    on/after Jun 1, and that date is the sale date.
Value = billing.baseAmount; rep = Atlas roster; source/events/calls enrich from GHL + Zoom.

Output is ONE flat list of rows, type "Package" | "Digital Diagnosis". Digital Diagnosis comes only from the GHL
stage scan and is never mixed into package totals (BRIEF §4).
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


def atlas_base_amount(atlas: dict) -> float | None:
    """The deal's value: Atlas billing.baseAmount, as is (Bob, 2026-10-15: the plain agreed amount, no intro-discount
    adjustment -- reps often forget to tick introPricing anyway). A one_time account carries its one-time total here.
    None when blank or 0 (a prepaid account may hold 0)."""
    base = (atlas.get("billing") or {}).get("baseAmount")
    return float(base) if base and base > 0 else None


def deal_basis(a: dict) -> tuple[str, str, bool] | None:
    """Is this Atlas account a sold deal, and when? -> (close date, basis, atlas_era), or None.
    A legacy import's createdAt is a BACKDATED import date, so it counts only via billing.firstChargeDate (on/after the
    audit start) -- and that field is a CALENDAR date stored as UTC midnight: read it as-is, never convert it to Pacific
    (2026-06-11T00:00Z would become Jun 10). Test/sponsor records never count."""
    if a["companyName"].lower() in cfg.EXCLUDED_ATLAS_ACCOUNTS:
        return None
    if a.get("legacy"):
        fcd = _day((a.get("billing") or {}).get("firstChargeDate"))
        return (fcd, "atlas first charge date", False) if fcd and fcd >= cfg.AUDIT_START else None
    created = la_day(a.get("createdAt"))
    return (created, "atlas created", True) if created and created >= cfg.ATLAS_ERA_START else None


def build_deals(data: dict, lk: Lookups) -> list[dict]:
    """One Package deal per qualifying Atlas account (see deal_basis). `data` keys: atlas, master_opps, ghl_users,
    roster (a Roster)."""
    ghl = _GhlIndex(data["master_opps"])
    deals = []
    for a in data["atlas"]:
        basis = deal_basis(a)
        if basis:
            deals.append(_one_deal(a, ghl.find(a, a["companyName"]), data, lk, close=basis[0], close_basis=basis[1], atlas_era=basis[2]))
    return deals


def _one_deal(a: dict, o: dict | None, data: dict, lk: Lookups, *, close: str, close_basis: str, atlas_era: bool) -> dict:
    """One row. `a` the Atlas account, `o` its ADV Master opp (if GHL has one)."""
    ghl_won = _day(o.get("lastStatusChangeAt")) if o else None
    roster: Roster = data["roster"]
    ghl_assigned = (data.get("ghl_users") or {}).get(o.get("assignedTo")) if o else None
    ghl_rep = roster.ghl_user_reps.get(o.get("assignedTo")) if o else None
    contact_name = (o["contact"]["name"] if o and o.get("contact") else None) or (a["users"][0]["name"] if a.get("users") else None)
    client = a["companyName"]
    billing = a.get("billing") or {}

    # ---- GHL contact attribution + calls: opp-linked contact when there is an opp, else a lookup by the Atlas
    # account's own contacts.
    gc = lk.contact_calls(o["contactId"]) if o and o.get("contactId") else {}
    tags = ((o.get("contact") or {}).get("tags") if o else None) or []
    src = (gc.get("contact") or {}).get("source") if gc else (o.get("source") if o else None)
    attr = (gc.get("contact") or {}).get("attributionSource") if gc else None
    contact_added = None
    if not o:
        ex = lk.find_contact(a)
        if ex:
            gc, tags, src, attr = {"calls": ex.get("calls") or [], "contact": ex}, ex.get("tags") or [], ex.get("source"), ex.get("attributionSource")
            contact_added = _day(ex.get("dateAdded"))

    base = atlas_base_amount(a)
    amount, amount_basis = (base, "Atlas billing") if base else (None, "none yet")

    # ---- source: an explicit Atlas referral partner outranks inferred tags
    partner, partner_id = referral_partner(a)
    if partner:
        src_info = {"source_type": "Referral", "source_detail": f"Atlas referral partner: {partner}", "event": None}
    elif o or gc.get("contact"):
        src_info = resolve_source(tags, src, attr, close)
    else:
        src_info = {"source_type": "Unknown", "source_detail": "no GHL match", "event": None}

    # ---- rep
    names = [contact_name, client] + [u.get("name") for u in a.get("users") or []]
    zoom = lk.zoom_hits([n for n in names if n], close)
    score = _call_evidence(gc.get("calls") or [], zoom, close, roster)
    owner, basis = _attribute_owner(a.get("salesRep"), ghl_rep, score, roster)

    # ---- flags (drive the dashboard's "needs a look" filters). no_first_charge_date / one_time are informational.
    flags = []
    if owner == cfg.NEEDS_REP:
        flags.append("needs_rep")
    if not amount:
        flags.append("no_amount")
    if not o and not gc.get("contact"):
        flags.append("no_ghl_match")
    if a.get("stage") == "closed":
        flags.append("atlas_closed")
    if score and owner != cfg.NEEDS_REP and score.most_common(1)[0][0] != owner:
        flags.append("owner_conflict")
    onboarding = a.get("onboarding") or {}
    if atlas_era and onboarding.get("total") and onboarding.get("completed", 0) < onboarding["total"]:
        flags.append("onboarding_incomplete")
    if atlas_era and not _day(billing.get("firstChargeDate")):
        flags.append("no_first_charge_date")
    if billing.get("billingType") == "one_time":
        flags.append("one_time")

    return {
        "key": f"atlas:{a['id']}", "type": PACKAGE, "client": client, "contact": contact_name,
        "close_date": close, "close_month": close[:7], "close_date_basis": close_basis,
        "amount": amount, "amount_basis": amount_basis, "base_amount": base,
        "billing_type": billing.get("billingType"), "billing_package": billing.get("package"), "first_charge_date": _day(billing.get("firstChargeDate")),
        "owner": owner, "owner_basis": basis, "call_score": dict(score),
        "noncore_assignee": ghl_assigned if ghl_assigned and not ghl_rep else None,
        **src_info, "referral_partner": partner, "referral_partner_id": partner_id, "tags": tags[:8], "where": None,
        "atlas_era": atlas_era, "atlas_id": a["id"], "atlas_stage": a.get("stage"), "atlas_created": la_day(a["createdAt"]),
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
            "base_amount": None, "billing_type": None, "billing_package": None, "first_charge_date": None, "referral_partner": None, "referral_partner_id": None,
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
    """Package deals (from Atlas) + Digital Diagnosis rows (from the GHL stage scan), de-duplicated by KEY only, newest
    first. Atlas ids are the identity now: two Atlas accounts with similar names are two clients, never merged by name
    (the old name-normalising dedupe existed only to paper over the billing-sheet join)."""
    seen: set = set()
    rows = []
    for r in deals + dd_rows:
        if r["key"] in seen:
            continue
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
