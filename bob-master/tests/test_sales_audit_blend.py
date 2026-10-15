from app.tasks import sales_audit_blend as b
from app.tasks import sales_audit_config as cfg

PAUL_GHL = "VcAw8YaXQU5UKKrQqmc7"
ADV = cfg.GHL_ADV_MASTER_PIPELINE


def _lookups(calls=None, zoom=None, extra=None):
    return b.Lookups(
        contact_calls=lambda cid: (calls or {}).get(cid, {}),
        find_contact=lambda a: extra,
        zoom_hits=lambda names, close: zoom or [],
    )


def _atlas(name, created, **kw):
    return {"id": f"id-{name}", "companyName": name, "createdAt": created, "stage": "onboarding",
            "users": [], "salesRep": None, "billing": None, "legacy": False,
            "onboarding": {"completed": 9, "confirmed": 9, "total": 9}, **kw}


def _opp(oid, name, status="won", won="2026-07-10T12:00:00Z", assigned=None, contact=None, value=3000):
    return {"id": oid, "name": name, "status": status, "lastStatusChangeAt": won, "lastStageChangeAt": won,
            "pipelineId": ADV, "pipelineStageId": "s-won", "contactId": f"c-{oid}", "assignedTo": assigned,
            "monetaryValue": value, "contact": {"name": name, "email": "", "phone": "", "tags": [], **(contact or {})}}


GHL_USERS = [{"id": PAUL_GHL, "name": "Paul Rastrelli", "email": "paul@advancedmarketers.co"},
             {"id": "u-simon", "name": "Simon Ting", "email": "simon@advancedmarketers.co"}]


def _roster(atlas_reps=None, ghl_users=None):
    """The Atlas sales roster as the blend sees it. Default = the four legacy reps; Simon is a GHL user who is NOT on it."""
    return b.Roster.from_atlas(atlas_reps if atlas_reps is not None else cfg.LEGACY_ROSTER, ghl_users if ghl_users is not None else GHL_USERS)


def _data(atlas=(), billing=(), opps=(), other=(), users=None, roster=None):
    return {"atlas": list(atlas), "billing": list(billing), "master_opps": list(opps), "other_opps": list(other),
            "stages": {"s-won": ("ADV - Master Pipeline", "Closed Won")}, "roster": roster or _roster(),
            "ghl_users": {PAUL_GHL: "Paul Rastrelli", "u-simon": "Simon Ting", **(users or {})}}


def _bill(name, month, amt, retainer="$3,500"):
    return {"account": name, "retainer": retainer, "months": {month: amt}}


def _rows(data, lk=None):
    return b.build_sales_rows(data, lk or _lookups())


# ---------------------------------------------------------------- the era rule

def test_atlas_era_account_is_always_a_package_closed_on_its_pacific_created_day():
    # 03:00 UTC Jul 5 is 8pm PDT Jul 4 -- the sale happened Jul 4.
    a = _atlas("Acme Roofing", "2026-07-05T03:00:00Z", billing={"baseAmount": 500, "introPricing": False})
    (row,) = _rows(_data(atlas=[a]))
    assert row["type"] == b.PACKAGE            # even at a $500 DD price point
    assert row["close_date"] == "2026-07-04"
    assert row["atlas_era"] is True


def test_legacy_import_backdated_into_window_is_not_a_sale():
    a = _atlas("Reel Electric", "2026-02-25T10:00:00Z", legacy=True)
    assert _rows(_data(atlas=[a])) == []


def test_legacy_flag_excludes_even_after_cutoff():
    a = _atlas("Late Import", "2026-07-20T10:00:00Z", legacy=True)
    assert _rows(_data(atlas=[a])) == []


def test_june_billed_client_with_backdated_atlas_record_uses_billing_not_atlas_date():
    a = _atlas("Reel Electric", "2026-02-25T10:00:00Z", legacy=True)
    (row,) = _rows(_data(atlas=[a], billing=[_bill("The Reel Electric Company", "June 2026", 2500)]))
    assert row["atlas_era"] is False
    assert row["close_month"] == "2026-06"      # not Feb
    assert row["close_date"] is None            # billing gives a month only
    assert "estimated_date" in row["flags"]


def test_billed_client_missing_from_atlas_is_kept_and_flagged():
    (row,) = _rows(_data(billing=[_bill("Ghost Plumbing", "August 2026", 4000)]))
    assert "not_in_atlas" in row["flags"]
    assert row["type"] == b.PACKAGE


def test_billing_before_audit_start_is_ignored():
    assert _rows(_data(billing=[_bill("Old Client", "May 2026", 3000)])) == []


def test_era_account_is_not_double_counted_from_billing():
    a = _atlas("Acme Roofing", "2026-07-10T20:00:00Z")
    rows = _rows(_data(atlas=[a], billing=[_bill("Acme Roofing LLC", "July 2026", 2500)]))
    assert len(rows) == 1 and rows[0]["atlas_era"]


# ---------------------------------------------------------------------- amounts

def test_amount_is_atlas_base_less_intro_and_sheet_is_only_a_spot_check():
    a = _atlas("Bracero", "2026-09-01T20:00:00Z", billing={"baseAmount": 3500, "introPricing": True})
    (row,) = _rows(_data(atlas=[a], billing=[_bill("Bracero", "September 2026", 2500)]))
    assert row["amount"] == 2500 and row["amount_basis"] == "Atlas billing"
    assert row["base_amount"] == 3500 and "amount_mismatch" not in row["flags"]


def test_amount_mismatch_is_flagged_not_overwritten():
    a = _atlas("Bracero", "2026-09-01T20:00:00Z", billing={"baseAmount": 3500, "introPricing": False})
    (row,) = _rows(_data(atlas=[a], billing=[_bill("Bracero", "September 2026", 2500)]))
    assert row["amount"] == 3500 and "amount_mismatch" in row["flags"] and row["sheet_first_charge"] == 2500


def test_falls_back_to_sheet_then_notes_when_atlas_billing_blank():
    a = _atlas("Prepaid Co", "2026-08-01T20:00:00Z", billing={"baseAmount": 0, "billingType": "prepaid"})
    (row,) = _rows(_data(atlas=[a], billing=[_bill("Prepaid Co", "August 2026", 6000)]))
    assert row["amount"] == 6000 and row["amount_basis"] == "billing sheet"
    n = _atlas("Notes Co", "2026-08-01T20:00:00Z", notes_unused=1, salesNotes="Closed at $4,200/mo")
    (row,) = _rows(_data(atlas=[n]))
    assert row["amount"] == 4200.0 and row["amount_basis"] == "Atlas notes (est.)"


def test_implausible_billing_cell_is_dropped_not_summed():
    first = b.first_charges([{"account": "Bad Cell", "retainer": "", "months": {"June 2026": 4111222233334444.0, "July 2026": 3000.0}}])
    assert first["badcell"]["first_month"] == "July 2026"     # the bogus June cell never counts
    assert b.first_charges([{"account": "Only Bad", "months": {"June 2026": 9_999_999.0}}]) == {}


# -------------------------------------------------------------------- attribution

def test_atlas_sales_rep_wins_when_a_core_rep():
    a = _atlas("Acme", "2026-07-10T20:00:00Z", salesRep={"name": "kyle kellner"})
    (row,) = _rows(_data(atlas=[a]))
    assert (row["owner"], row["owner_basis"]) == ("Kyle Kellner", "Atlas salesRep")


def test_non_core_atlas_rep_is_never_credited():
    a = _atlas("Acme", "2026-07-10T20:00:00Z", salesRep={"name": "Simon Ting"})
    (row,) = _rows(_data(atlas=[a]))
    assert row["owner"] == cfg.NEEDS_REP and "needs_rep" in row["flags"]


def test_ghl_core_assignee_credited_but_simon_ignored_and_recorded():
    a = _atlas("Acme", "2026-07-10T20:00:00Z", users=[{"name": "Al", "email": "al@acme.com", "phone": ""}])
    o = _opp("o1", "Al", assigned="u-simon", contact={"email": "al@acme.com"})
    (row,) = _rows(_data(atlas=[a], opps=[o]))
    assert row["owner"] == cfg.NEEDS_REP and row["noncore_assignee"] == "Simon Ting"
    o2 = _opp("o2", "Al", assigned=PAUL_GHL, contact={"email": "al@acme.com"})
    (row,) = _rows(_data(atlas=[a], opps=[o2]))
    assert (row["owner"], row["owner_basis"]) == ("Paul Rastrelli", "GHL opp assignee")


def test_call_evidence_fills_in_when_nobody_is_assigned_and_conflict_is_flagged():
    a = _atlas("Acme", "2026-07-10T20:00:00Z", salesRep={"name": "Kyle Kellner"})
    zoom = [{"rep": "Jaden Bashaw", "date": "2026-07-08", "min": 40, "topic": "Acme x AM", "via": "topic"}]
    (row,) = _rows(_data(atlas=[a]), _lookups(zoom=zoom))
    assert row["owner"] == "Kyle Kellner" and "owner_conflict" in row["flags"]
    b2 = _atlas("Beta", "2026-07-10T20:00:00Z")
    (row,) = _rows(_data(atlas=[b2]), _lookups(zoom=zoom))
    assert (row["owner"], row["owner_basis"]) == ("Jaden Bashaw", "call activity (Zoom/GHL)")


# ------------------------------------------------------------------------ source

def test_atlas_referral_partner_outranks_tags_and_accepts_string_or_object():
    for rp in ("Partner Pat", {"name": "Partner Pat"}):
        a = _atlas("Acme", "2026-07-10T20:00:00Z", referralPartner=rp)
        (row,) = _rows(_data(atlas=[a]))
        assert row["source_type"] == "Referral" and row["referral_partner"] == "Partner Pat"


def test_referral_partner_id_is_carried_for_grouping_and_may_be_null():
    a = _atlas("Acme", "2026-07-10T20:00:00Z", referralPartner={"name": "Partner Pat", "partnerId": "p-9"})
    (row,) = _rows(_data(atlas=[a]))
    assert (row["referral_partner"], row["referral_partner_id"]) == ("Partner Pat", "p-9")
    a = _atlas("Beta", "2026-07-10T20:00:00Z", referralPartner={"name": "Partner Pat", "partnerId": None})
    (row,) = _rows(_data(atlas=[a]))
    assert row["source_type"] == "Referral" and row["referral_partner_id"] is None


def test_null_referral_partner_falls_back_to_ghl_tags():
    a = _atlas("Acme", "2026-07-10T20:00:00Z", referralPartner=None, users=[{"name": "Al", "email": "al@acme.com", "phone": ""}])
    o = _opp("o1", "Al", contact={"email": "al@acme.com", "tags": ["referral"]})
    (row,) = _rows(_data(atlas=[a], opps=[o]))
    assert row["source_type"] == "Referral" and row["referral_partner"] is None


def test_missing_referral_field_is_fine():
    (row,) = _rows(_data(atlas=[_atlas("Acme", "2026-07-10T20:00:00Z")]))
    assert row["referral_partner"] is None


def test_retro_event_tag_after_close_is_ignored():
    # Closed Jul 10; Dispatch 01 was Sept 25 -> the tag post-dates the sale.
    src, _ = b.classify_source(["dispatch"], None, None)
    assert src == "Event"
    out = b.resolve_source(["dispatch"], None, None, "2026-07-10")
    assert out["source_type"] != "Event" and out["event"] is None and "post-date close" in out["source_detail"]


def test_event_before_close_counts_and_latest_qualifying_wins():
    out = b.resolve_source(["raw may 8", "may 15 attendee"], None, None, "2026-07-10")
    assert out["source_type"] == "Event" and out["event"] == "May 15 event"


def test_source_priority_referral_before_paid():
    assert b.classify_source(["referral", "meta lead"], None, None)[0] == "Referral"
    assert b.classify_source(["meta lead"], None, None)[0] == "Paid Ads"
    assert b.classify_source([], None, None)[0] == "Unknown"


# ------------------------------------------------------------------ onboarding flag

def test_incomplete_onboarding_is_a_soft_flag_not_an_exclusion():
    a = _atlas("Acme", "2026-07-10T20:00:00Z", onboarding={"completed": 3, "confirmed": 1, "total": 9})
    (row,) = _rows(_data(atlas=[a]))
    assert "onboarding_incomplete" in row["flags"]


# ------------------------------------------------------------------ Digital Diagnosis

def test_dd_by_stage_dedupes_same_person_across_pipelines_within_14_days():
    stages = {"s-dd": ("RAW", "SOLD -Digital Diagnosis"), "s-dd2": ("DISPATCH", "Digital Diagnosis")}
    mk = lambda oid, stage, d: {**_opp(oid, "Jane Smith", status="won", won=d, value=500), "pipelineStageId": stage}
    d = _data(opps=[mk("a", "s-dd", "2026-08-01T10:00:00Z")], other=[mk("b", "s-dd2", "2026-08-05T10:00:00Z")])
    d["stages"].update(stages)
    rows = b.build_dd_rows(d)
    assert len(rows) == 1 and rows[0]["type"] == b.DIGITAL_DIAGNOSIS


def test_dd_owner_requires_core_rep_and_non_core_is_needs_rep():
    d = _data(other=[{**_opp("a", "Jane Smith", assigned="u-simon", value=500), "pipelineStageId": "s-won"}])
    (row,) = b.build_dd_rows(d)
    assert row["owner"] == cfg.NEEDS_REP and row["noncore_assignee"] == "Simon Ting"


def test_non_era_billing_deal_at_dd_price_lands_in_dd_never_in_packages():
    rows = _rows(_data(billing=[_bill("Quick Dx Client", "August 2026", 500.0, retainer="")]))
    assert [r["type"] for r in rows] == [b.DIGITAL_DIAGNOSIS]


def test_duplicate_client_is_listed_once():
    rows = _rows(_data(billing=[_bill("OG Plumbing", "August 2026", 5000), _bill("OG Plumbing Inc", "June 2026", 5000)]))
    assert len([r for r in rows if r["type"] == b.PACKAGE]) == 1


# ------------------------------------------------------------------------ helpers

def test_vtt_minutes_reads_last_cue_end():
    vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:05.000\nhi\n\n2\n01:02:00.000 --> 01:02:59.900\nbye\n"
    assert b.vtt_minutes(vtt) == 62 and b.vtt_minutes("") == 0


def test_same_name_needs_two_tokens_for_subset_match():
    assert b.same_name("Mike Jones", "Jones Mike") and b.same_name("Mike Jones Jr", "Mike Jones")
    assert not b.same_name("Mike", "Mike Jones")


# ----------------------------------------------------------------- roster = Atlas's sales list

def test_names_are_tidied_but_mixed_case_is_left_alone():
    assert b.tidy_name("Chris paniagua") == "Chris Paniagua" and b.tidy_name("Mary McDonald") == "Mary McDonald"
    assert b.tidy_name("  kyle   kellner ") == "Kyle Kellner" and b.tidy_name(None) == ""


def test_roster_from_atlas_lowercases_emails_dedupes_and_drops_emailless_users():
    r = b.Roster.from_atlas([{"name": "chris paniagua", "email": "Chris@AdvancedMarketers.co"},
                             {"name": "Chris again", "email": "chris@advancedmarketers.co"}, {"name": "No Email", "email": ""}])
    assert r.reps == [{"name": "Chris Paniagua", "email": "chris@advancedmarketers.co"}]


def test_resolve_prefers_email_then_name_and_returns_none_for_strangers():
    r = _roster()
    assert r.resolve(email="KYLEKELLNER@advancedmarketers.co", name="whoever") == "Kyle Kellner"
    assert r.resolve(name="  jaden   BASHAW ") == "Jaden Bashaw"
    assert r.resolve(email="x@y.co", name="Nobody") is None and r.resolve() is None


def test_ghl_users_map_to_roster_reps_by_email_then_name_and_strangers_stay_unmapped():
    r = b.Roster.from_atlas(cfg.LEGACY_ROSTER, [
        {"id": "g1", "name": "P. R.", "email": "paul@advancedmarketers.co"},          # email wins over a different name
        {"id": "g2", "name": "Kyle Kellner", "email": ""},                              # name fallback
        {"id": "g3", "name": "Simon Ting", "email": "simon@advancedmarketers.co"}])     # not on the roster
    assert r.ghl_user_reps == {"g1": "Paul Rastrelli", "g2": "Kyle Kellner"}


def test_a_new_rep_added_in_atlas_is_credited_at_every_tier_with_no_code_change():
    new = {"name": "Anthony Aguilar", "email": "anthonyaguilar@advancedmarketers.co"}
    roster = _roster(cfg.LEGACY_ROSTER + [new], GHL_USERS + [{"id": "u-ant", "name": "Anthony A", "email": "anthonyaguilar@advancedmarketers.co"}])
    # tier 1: Atlas salesRep (email match; name casing in Atlas is irrelevant)
    a = _atlas("Acme", "2026-07-10T20:00:00Z", salesRep={"name": "anthony aguilar", "email": "AnthonyAguilar@advancedmarketers.co"})
    (row,) = _rows(_data(atlas=[a], roster=roster))
    assert (row["owner"], row["owner_basis"]) == ("Anthony Aguilar", "Atlas salesRep")
    # tier 2: GHL opp assignee, mapped by email
    a2 = _atlas("Beta", "2026-07-10T20:00:00Z", users=[{"name": "Al", "email": "al@beta.com", "phone": ""}])
    o = _opp("o9", "Al", assigned="u-ant", contact={"email": "al@beta.com"})
    (row,) = _rows(_data(atlas=[a2], opps=[o], users={"u-ant": "Anthony A"}, roster=roster))
    assert (row["owner"], row["owner_basis"]) == ("Anthony Aguilar", "GHL opp assignee")
    # tier 3: call evidence (Zoom host resolved to the roster name by the index)
    zoom = [{"rep": "Anthony Aguilar", "date": "2026-07-08", "min": 30, "topic": "Gamma x AM", "via": "topic"}]
    (row,) = _rows(_data(atlas=[_atlas("Gamma", "2026-07-10T20:00:00Z")], roster=roster), _lookups(zoom=zoom))
    assert (row["owner"], row["owner_basis"]) == ("Anthony Aguilar", "call activity (Zoom/GHL)")


def test_there_is_no_exclusion_list_if_atlas_lists_simon_he_is_credited():
    """Bob (2026-10-15): the Atlas endpoint is the level of truth; the old Simon/Allondra/Makayla exclusion was only for manual debug pulls."""
    roster = _roster(cfg.LEGACY_ROSTER + [{"name": "Simon Ting", "email": "simon@advancedmarketers.co"}])
    a = _atlas("Acme", "2026-07-10T20:00:00Z", salesRep={"name": "Simon Ting", "email": "simon@advancedmarketers.co"})
    (row,) = _rows(_data(atlas=[a], roster=roster))
    assert row["owner"] == "Simon Ting" and row["owner_basis"] == "Atlas salesRep"


def test_a_rep_removed_from_the_atlas_roster_is_no_longer_credited():
    roster = _roster([r for r in cfg.LEGACY_ROSTER if r["name"] != "Kyle Kellner"])
    a = _atlas("Acme", "2026-07-10T20:00:00Z", salesRep={"name": "Kyle Kellner", "email": "kylekellner@advancedmarketers.co"})
    (row,) = _rows(_data(atlas=[a], roster=roster))
    assert row["owner"] == cfg.NEEDS_REP and "needs_rep" in row["flags"]
