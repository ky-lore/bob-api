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


def _data(atlas=(), opps=(), other=(), users=None, roster=None):
    return {"atlas": list(atlas), "master_opps": list(opps), "other_opps": list(other),
            "stages": {"s-won": ("ADV - Master Pipeline", "Closed Won")}, "roster": roster or _roster(),
            "ghl_users": {PAUL_GHL: "Paul Rastrelli", "u-simon": "Simon Ting", **(users or {})}}



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






# ---------------------------------------------------------------------- amounts





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


# ------------------------------------------------ Atlas is the only source: value, legacy dating, no sheet

def _legacy(name, fcd, **kw):
    return _atlas(name, "2026-02-25T10:00:00Z", legacy=True, billing={"baseAmount": 2500, "firstChargeDate": fcd}, **kw)


def test_value_is_the_plain_base_amount_even_with_intro_pricing_on():
    a = _atlas("Teton", "2026-09-28T20:00:00Z", billing={"baseAmount": 3500, "introPricing": True})
    (row,) = _rows(_data(atlas=[a]))
    assert row["amount"] == 3500 and row["base_amount"] == 3500 and row["amount_basis"] == "Atlas billing"


def test_blank_or_zero_base_means_no_amount_and_notes_are_never_used_as_a_guess():
    for billing in (None, {"baseAmount": 0, "billingType": "prepaid"}, {"baseAmount": None}):
        a = _atlas("Gamma", "2026-08-01T20:00:00Z", billing=billing, salesNotes="Closed at $4,200/mo")
        (row,) = _rows(_data(atlas=[a]))
        assert row["amount"] is None and row["amount_basis"] == "none yet" and "no_amount" in row["flags"]


def test_a_one_time_deal_keeps_its_total_and_is_flagged_one_time():
    a = _atlas("Bernie", "2026-07-20T20:00:00Z", billing={"baseAmount": 6500, "billingType": "one_time"})
    (row,) = _rows(_data(atlas=[a]))
    assert row["amount"] == 6500 and row["billing_type"] == "one_time" and "one_time" in row["flags"] and row["type"] == b.PACKAGE


def test_billing_type_and_package_tier_are_carried_on_the_row():
    a = _atlas("Aspen", "2026-08-05T20:00:00Z", billing={"baseAmount": 499, "billingType": "recurring", "package": "website_only"})
    (row,) = _rows(_data(atlas=[a]))
    assert (row["amount"], row["billing_type"], row["billing_package"], row["type"]) == (499, "recurring", "website_only", b.PACKAGE)   # $499 is a package, never DD


def test_a_legacy_import_counts_via_its_first_charge_date_not_its_backdated_created_date():
    a = _legacy("Cortex", "2026-06-11T00:00:00.000Z")
    (row,) = _rows(_data(atlas=[a]))
    assert row["close_date"] == "2026-06-11" and row["close_month"] == "2026-06"            # the calendar date as stored
    assert row["close_date_basis"] == "atlas first charge date" and row["atlas_era"] is False and row["type"] == b.PACKAGE
    assert row["first_charge_date"] == "2026-06-11" and "onboarding_incomplete" not in row["flags"]


def test_first_charge_date_is_a_calendar_date_never_shifted_into_pacific_time():
    """Atlas stores 2026-06-11 as UTC midnight; converting that instant to Pacific would give June 10 (and 2026-07-01 -> Jun 30)."""
    (row,) = _rows(_data(atlas=[_legacy("Edge", "2026-07-01T00:00:00.000Z")]))
    assert row["close_date"] == "2026-07-01"


def test_a_legacy_import_with_no_first_charge_date_or_one_before_june_is_not_a_deal():
    assert _rows(_data(atlas=[_atlas("NoDate", "2026-02-25T10:00:00Z", legacy=True, billing={"baseAmount": 2500})])) == []
    assert _rows(_data(atlas=[_legacy("OldClient", "2026-05-31T00:00:00.000Z")])) == []
    assert _rows(_data(atlas=[_legacy("JuneFirst", "2026-06-01T00:00:00.000Z")]))[0]["close_date"] == "2026-06-01"


def test_a_non_legacy_account_created_before_july_is_not_a_deal_and_the_boundary_is_the_pacific_day():
    assert _rows(_data(atlas=[_atlas("Early", "2026-06-23T20:00:00Z")])) == []
    assert _rows(_data(atlas=[_atlas("Boundary", "2026-07-01T05:00:00Z")])) == []          # 10pm PDT Jun 30 -> still June
    assert _rows(_data(atlas=[_atlas("Boundary2", "2026-07-01T08:00:00Z")]))[0]["close_date"] == "2026-07-01"


def test_nothing_but_an_atlas_account_can_create_a_package_deal():
    opp = _opp("o1", "Ghost Co", status="won", won="2026-08-01T10:00:00Z", value=2500)
    assert [r for r in _rows(_data(opps=[opp])) if r["type"] == b.PACKAGE] == []


def test_two_distinct_atlas_accounts_with_similar_names_are_two_deals():
    rows = _rows(_data(atlas=[_atlas("OG Plumbing", "2026-07-10T20:00:00Z"), _atlas("OG Plumbing Inc", "2026-07-11T20:00:00Z")]))
    assert len(rows) == 2 and len({r["key"] for r in rows}) == 2


def test_no_first_charge_date_is_flagged_only_for_atlas_era_accounts():
    era_missing = _atlas("A", "2026-07-10T20:00:00Z", billing={"baseAmount": 2500})
    era_set = _atlas("B", "2026-07-10T20:00:00Z", billing={"baseAmount": 2500, "firstChargeDate": "2026-07-12T00:00:00.000Z"})
    rows = {r["client"]: r for r in _rows(_data(atlas=[era_missing, era_set, _legacy("C", "2026-06-11T00:00:00.000Z")]))}
    assert "no_first_charge_date" in rows["A"]["flags"] and "no_first_charge_date" not in rows["B"]["flags"] and "no_first_charge_date" not in rows["C"]["flags"]
