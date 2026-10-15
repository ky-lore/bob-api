"""ZoomIndex matching rules (the billing-sheet reader this file used to test was retired 2026-10-15)."""
from app.tasks import sales_audit_sources as src


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
