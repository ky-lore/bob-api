"""
Source readers for the sales audit (2026-10-15): everything that touches the
network or the DB, kept apart from sales_audit_blend.py's pure rules. Each
returns plain dicts/lists in the shape the blend expects.

Zoom is NOT pulled here. bob-api's daily zoom_call_sync already stores every
transcript in ZoomCallRecord (the prototype's 25-minute full pull and its
"recordings get auto-deleted" worry both disappear by reading that table).
Dependency to know about: the table only has history from whenever the sync
(or backfill_zoom_calls) was run, so a first deploy needs a one-time backfill
back to ~Jun 2026 or early deals will show no Zoom evidence.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models import ZoomCallRecord
from app.tasks import sales_audit_config as cfg
from app.tasks.sales_audit_blend import Lookups, phone10, vtt_minutes

logger = logging.getLogger(__name__)

# Far enough back to cover every deal's pre-close sales calls.
_ZOOM_LOOKBACK_START = dt.datetime(2026, 4, 1)


# ------------------------------------------------------------------- billing

_MONEY = re.compile(r"-?[\d,]+(?:\.\d+)?")


def parse_money(cell: str | None) -> float | None:
    if not cell:
        return None
    m = _MONEY.search(cell.replace("$", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_billing_rows(header: list[str], rows: list[list[str]]) -> tuple[list[dict], int]:
    """(billing records, count of cells dropped as implausible). `header` is
    [key header, *selected headers]; rows align to it. Cells above
    MAX_PLAUSIBLE_MONTHLY_CHARGE are dropped and only COUNTED -- never
    logged or returned, since the one seen in the prototype's snapshot was a
    16-digit card-number-shaped value."""
    month_re = re.compile(cfg.BILLING_MONTH_HEADER_REGEX)
    retainer_re = re.compile(cfg.BILLING_RETAINER_HEADER_REGEX, re.I)
    out, dropped = [], 0
    for row in rows:
        name = (row[0] if row else "").strip()
        if not name:
            continue
        months: dict[str, float] = {}
        retainer = None
        for h, cell in zip(header[1:], row[1:]):
            if month_re.match(h):
                amt = parse_money(cell)
                if amt is None or amt <= 0:
                    continue
                if amt > cfg.MAX_PLAUSIBLE_MONTHLY_CHARGE:
                    dropped += 1
                    continue
                months[h] = amt
            elif retainer_re.match(h) and cell:
                retainer = cell.strip()
        out.append({"account": name, "retainer": retainer, "months": months})
    return out, dropped


def read_billing(drive) -> tuple[list[dict], dict]:
    """Billing sheet -> [{account, retainer, months{'October 2026': 3500.0}}].
    Reads ONLY column A + month/retainer columns (see
    GoogleDriveClient.read_sheet_columns_by_header for why)."""
    tab = drive.tab_title_for_gid(cfg.BILLING_SHEET_ID, cfg.BILLING_SHEET_GID)
    month_re = re.compile(cfg.BILLING_MONTH_HEADER_REGEX)
    retainer_re = re.compile(cfg.BILLING_RETAINER_HEADER_REGEX, re.I)
    header, rows = drive.read_sheet_columns_by_header(
        cfg.BILLING_SHEET_ID, tab, lambda h: bool(month_re.match(h) or retainer_re.match(h.strip())))
    records, dropped = parse_billing_rows(header, rows)
    if dropped:
        logger.warning("sales-audit: dropped %d implausible billing cell(s) above $%s", dropped, cfg.MAX_PLAUSIBLE_MONTHLY_CHARGE)
    return records, {"columns_read": len(header) - 1, "rows": len(records), "implausible_cells_dropped": dropped}


# ----------------------------------------------------------------------- GHL

def fetch_ghl(ghl) -> dict:
    """ADV Master opps (all statuses), the two DD-only pipelines' opps, the
    stage id -> (pipeline, stage) map, and GHL user id -> name."""
    return {
        "master_opps": ghl.list_opportunities(cfg.GHL_ADV_MASTER_PIPELINE),
        "other_opps": [o for pid in cfg.GHL_DD_PIPELINES for o in ghl.list_opportunities(pid)],
        "stages": ghl.list_pipeline_stages(),
        "ghl_users": ghl.list_users(),
    }


# ------------------------------------------------------------------- lookups

class ZoomIndex:
    """In-memory index of the four reps' stored transcripts, built once per
    run. hits() is the prototype's rule (BRIEF §3.9): a transcript counts for
    a deal if its topic contains the contact/client name, or the text
    mentions the name >=3 times; only meetings on/before close + 10 days."""

    def __init__(self, entries: list[dict]):
        self.entries = entries

    @classmethod
    def from_db(cls, db: Session) -> "ZoomIndex":
        rows = (
            db.query(ZoomCallRecord.host_email, ZoomCallRecord.topic, ZoomCallRecord.start_time, ZoomCallRecord.transcript_text)
            .filter(ZoomCallRecord.start_time >= _ZOOM_LOOKBACK_START)
            .filter(ZoomCallRecord.host_email.in_(list(cfg.REPS_BY_ZOOM_EMAIL)))
            .all()
        )
        return cls([{
            "rep": cfg.REPS_BY_ZOOM_EMAIL[r.host_email.lower()] if r.host_email.lower() in cfg.REPS_BY_ZOOM_EMAIL else None,
            "topic": r.topic or "", "topic_lc": (r.topic or "").lower(),
            "date": r.start_time.date().isoformat(), "min": vtt_minutes(r.transcript_text),
            "text": (r.transcript_text or "").lower(),
        } for r in rows if r.host_email and r.host_email.lower() in cfg.REPS_BY_ZOOM_EMAIL])

    def hits(self, names: list[str], close: str | None) -> list[dict]:
        tokens = [n.lower() for n in names if n and len(n) >= 5]
        if not tokens:
            return []
        horizon = (dt.date.fromisoformat(close) + dt.timedelta(days=10)).isoformat() if close else None
        out = []
        for e in self.entries:
            if horizon and e["date"] > horizon:
                continue
            topic_hit = any(t in e["topic_lc"] for t in tokens)
            text_hit = (not topic_hit) and sum(e["text"].count(t) for t in tokens) >= 3
            if topic_hit or text_hit:
                out.append({"rep": e["rep"], "date": e["date"], "min": e["min"], "topic": e["topic"],
                            "via": "topic" if topic_hit else "text"})
        return out


def make_lookups(ghl, zoom: ZoomIndex) -> Lookups:
    """Lazy, memoized GHL lookups -- call history is fetched only for contacts
    that actually end up behind a deal."""
    contact_cache: dict[str, dict] = {}

    def contact_calls(contact_id: str) -> dict:
        if contact_id not in contact_cache:
            try:
                contact = ghl.get_contact(contact_id).get("contact", {})
                calls = ghl.contact_call_history(contact_id)
            except Exception:
                logger.exception("sales-audit: GHL contact %s lookup failed", contact_id)
                contact, calls = {}, []
            contact_cache[contact_id] = {
                "contact": {k: contact.get(k) for k in
                            ("dateAdded", "source", "attributionSource", "lastAttributionSource", "tags", "companyName", "assignedTo")},
                "calls": calls,
            }
        return contact_cache[contact_id]

    def find_contact(atlas: dict) -> dict | None:
        """An Atlas account with no matching opp: find its GHL contact by the
        account's own people (email -> phone -> name), then pull its calls."""
        probes = []
        for u in (atlas.get("users") or []) + [{"email": "", "phone": atlas.get("phone"), "name": ""}]:
            probes += [u.get("email"), phone10(u.get("phone")), u.get("name")]
        for q in probes:
            if not q or len(q) < 5:
                continue
            try:
                found = ghl.search_contact(q)
            except Exception:
                logger.exception("sales-audit: GHL contact search failed")
                return None
            if found:
                try:
                    calls = ghl.contact_call_history(found["id"])
                except Exception:
                    logger.exception("sales-audit: GHL call history failed")
                    calls = []
                return {"contactId": found["id"], "name": found.get("contactName"), "tags": found.get("tags"),
                        "source": found.get("source"), "dateAdded": found.get("dateAdded"),
                        "attributionSource": found.get("attributionSource"), "calls": calls}
        return None

    return Lookups(contact_calls=contact_calls, find_contact=find_contact, zoom_hits=zoom.hits)
