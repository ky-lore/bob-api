import datetime as dt

from app.tasks import sales_audit_sources as src
from app.tasks import sales_audit_config as cfg


def test_parse_money_handles_currency_formats():
    assert src.parse_money("$3,500.00") == 3500.0 and src.parse_money("1250") == 1250.0
    assert src.parse_money("") is None and src.parse_money("n/a") is None and src.parse_money(None) is None


def test_billing_rows_keep_months_and_retainer_and_drop_implausible_cells_without_returning_them():
    header = ["Client", "Rentainer", "September 2026", "October 2026"]
    rows = [["Acme", "$3,500", "$2,500", ""], ["Card Leak Co", "$1,000", "4111111111111111", "$1,000"], ["", "", "", ""]]
    records, dropped = src.parse_billing_rows(header, rows)
    assert dropped == 1 and [r["account"] for r in records] == ["Acme", "Card Leak Co"]
    assert records[0]["months"] == {"September 2026": 2500.0} and records[0]["retainer"] == "$3,500"
    assert records[1]["months"] == {"October 2026": 1000.0}
    assert "4111111111111111" not in repr(records)


def test_billing_header_allowlist_never_matches_card_columns():
    import re
    month, ret = re.compile(cfg.BILLING_MONTH_HEADER_REGEX), re.compile(cfg.BILLING_RETAINER_HEADER_REGEX, re.I)
    ok = lambda h: bool(month.match(h) or ret.match(h.strip()))
    assert ok("October 2026") and ok("Rentainer") and ok("Retainer")
    for bad in ("Card Number", "CVV", "Exp", "Name on card", "Billing Zip", "Notes", ""):
        assert not ok(bad)


class _FakeDrive:
    def __init__(self):
        self.asked = None

    def tab_title_for_gid(self, file_id, gid):
        return "Billing"

    def read_sheet_columns_by_header(self, file_id, tab, wanted):
        self.asked = [h for h in ("Client", "Card Number", "CVV", "Rentainer", "July 2026", "Notes") if wanted(h)]
        return ["Client", "Rentainer", "July 2026"], [["Acme", "$3,000", "$2,000"]]


def test_read_billing_requests_only_allowlisted_headers():
    drive = _FakeDrive()
    records, meta = src.read_billing(drive)
    assert drive.asked == ["Rentainer", "July 2026"]            # no card columns, no notes, not even the key header
    assert records == [{"account": "Acme", "retainer": "$3,000", "months": {"July 2026": 2000.0}}]
    assert meta["rows"] == 1 and meta["implausible_cells_dropped"] == 0


def _zoom(*entries):
    return src.ZoomIndex([{"rep": "Kyle Kellner", "topic": t, "topic_lc": t.lower(), "date": d, "min": m, "text": x.lower()} for t, d, m, x in entries])


def test_zoom_matches_on_topic_or_three_text_mentions_within_close_plus_10():
    z = _zoom(("Acme Roofing x AM Discovery", "2026-07-02", 45, ""),
              ("Personal room", "2026-07-03", 30, "acme roofing ... acme roofing ... acme roofing"),
              ("Personal room", "2026-07-03", 30, "acme roofing once only"),
              ("Acme Roofing follow up", "2026-07-20", 20, ""))                      # after close+10
    hits = z.hits(["Acme Roofing"], "2026-07-05")
    assert [(h["via"], h["min"]) for h in hits] == [("topic", 45), ("text", 30)]
    assert z.hits(["Al"], "2026-07-05") == []                                         # <5 chars never matches
    assert len(z.hits(["Acme Roofing"], None)) == 3                                    # no close date -> no date gate
