"""
Tests app.tasks.cmdctr_report against the same fake clients test_atlas_report.py
uses -- proves the pipeline-stage account filter actually reaches
build_atlas_report, the loosened batch_size/max_tokens_cap constants get
threaded through, the CmdctrRun persistence is separate from AtlasReportRun
(so a narrow high-frequency run can never become what /pulse renders), and
the same soft-fail-on-push-error contract run_and_store_atlas_report has.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.tasks.atlas_report as atlas_report_mod
import app.tasks.cmdctr_report as mod
from app.db import Base
from app.models import AtlasReportRun, CmdctrRun


def _atlas_account(company_name, *, atlas_id=None, stage="live"):
    created_at = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "id": atlas_id or company_name.lower().replace(" ", "-"),
        "companyName": company_name,
        "stage": stage,
        "isActive": True,
        "createdAt": created_at,
        "integrations": {},
    }


class _FakeAtlasClient:
    accounts: list = []
    pulse_run_calls: list = []
    pulse_run_raises: Exception | None = None

    def get_all_accounts(self):
        return _FakeAtlasClient.accounts

    def post_pulse_run(self, payload):
        _FakeAtlasClient.pulse_run_calls.append(payload)
        if _FakeAtlasClient.pulse_run_raises:
            raise _FakeAtlasClient.pulse_run_raises
        return {}


class _FakeClickUp:
    def get_folder_lists(self, folder_id):
        return []


class _FakeSlack:
    def channel_history(self, channel_id, oldest_ts=None):
        return []


class _FakeAdsClient:
    def get_account_spend(self, account_id, date_range="YESTERDAY"):
        raise RuntimeError("no fake response")


def _setup(monkeypatch):
    # cmdctr_report.py delegates all gathering to build_atlas_report, so the
    # fakes are patched on atlas_report_mod (what build_atlas_report actually
    # imports/calls), not cmdctr_report's own module namespace.
    monkeypatch.setattr(atlas_report_mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(atlas_report_mod, "ClickUpClient", _FakeClickUp)
    monkeypatch.setattr(atlas_report_mod, "SlackClient", _FakeSlack)
    monkeypatch.setattr(atlas_report_mod, "GoogleAdsClient", _FakeAdsClient)
    monkeypatch.setattr(atlas_report_mod, "MetaAdsClient", _FakeAdsClient)
    # AtlasClient is imported separately into cmdctr_report.py's own
    # namespace too (for post_pulse_run) -- patch both call sites.
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    _FakeAtlasClient.accounts = []
    _FakeAtlasClient.pulse_run_calls = []
    _FakeAtlasClient.pulse_run_raises = None


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _fake_synthesize(accounts, on_batch_done=None, **kwargs):
    return {a["account"]: {"health": "on_track", "status": "x", "recent_work": "y"} for a in accounts}, []


def test_build_cmdctr_report_only_includes_pipeline_stage_accounts(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(atlas_report_mod, "synthesize_account_reports", _fake_synthesize)
    _FakeAtlasClient.accounts = [
        _atlas_account("Onboarding Co", stage="onboarding"),
        _atlas_account("Development Co", stage="development"),
        _atlas_account("Live Co", stage="live"),
        _atlas_account("At Risk Co", stage="at_risk"),
        _atlas_account("Closed Co", stage="closed"),
    ]

    records, _ = mod.build_cmdctr_report()

    assert {r["company_name"] for r in records} == {"Onboarding Co", "Development Co"}


def test_build_cmdctr_report_passes_the_loosened_batch_settings(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_atlas_account("Onboarding Co", stage="onboarding")]
    captured = {}

    def _capturing_synthesize(accounts, on_batch_done=None, **kwargs):
        captured.update(kwargs)
        return _fake_synthesize(accounts)

    monkeypatch.setattr(atlas_report_mod, "synthesize_account_reports", _capturing_synthesize)

    mod.build_cmdctr_report()

    assert captured == {"batch_size": mod._CMDCTR_BATCH_SIZE, "max_tokens_cap": mod._CMDCTR_MAX_TOKENS_CAP}


def test_run_and_push_cmdctr_report_persists_to_cmdctr_runs_not_atlas_report_runs(monkeypatch, db_session):
    _setup(monkeypatch)
    monkeypatch.setattr(atlas_report_mod, "synthesize_account_reports", _fake_synthesize)
    _FakeAtlasClient.accounts = [_atlas_account("Onboarding Co", stage="onboarding")]

    run = mod.run_and_push_cmdctr_report(db_session)

    assert isinstance(run, CmdctrRun)
    assert db_session.query(CmdctrRun).filter_by(id=run.id).one() is not None
    # The whole point: a narrow CMDCTR run must never be able to become
    # what GET .../latest (the full-universe exec dashboard) serves.
    assert db_session.query(AtlasReportRun).count() == 0

    data = json.loads(run.report_json)
    assert data["count"] == 1
    assert data["accounts"][0]["company_name"] == "Onboarding Co"


def test_run_and_push_cmdctr_report_pushes_to_atlas_and_records_pulse_push(monkeypatch, db_session):
    _setup(monkeypatch)
    monkeypatch.setattr(atlas_report_mod, "synthesize_account_reports", _fake_synthesize)
    _FakeAtlasClient.accounts = [_atlas_account("Onboarding Co", stage="onboarding")]

    run = mod.run_and_push_cmdctr_report(db_session)

    assert len(_FakeAtlasClient.pulse_run_calls) == 1
    pushed = _FakeAtlasClient.pulse_run_calls[0]
    assert pushed["runId"] == run.id
    assert pushed["count"] == 1
    assert json.loads(run.report_json)["pulse_push"] == {"ok": True, "error": None}


def test_run_and_push_cmdctr_report_soft_fails_when_the_pulse_push_errors(monkeypatch, db_session):
    _setup(monkeypatch)
    monkeypatch.setattr(atlas_report_mod, "synthesize_account_reports", _fake_synthesize)
    _FakeAtlasClient.accounts = [_atlas_account("Onboarding Co", stage="onboarding")]
    _FakeAtlasClient.pulse_run_raises = RuntimeError("Atlas is down")

    run = mod.run_and_push_cmdctr_report(db_session)

    assert run.id is not None
    assert db_session.query(CmdctrRun).filter_by(id=run.id).one() is not None
    assert json.loads(run.report_json)["pulse_push"] == {"ok": False, "error": "Atlas is down"}
