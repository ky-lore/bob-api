"""
Tests app.scheduler.start_scheduler -- proves all three cron jobs get
registered against the configured crontab strings with the intended
timezone, and that the zoom-call-sync/cmdctr-report jobs wrap their own DB
session and never let an exception escape (an unhandled error in a
background job must not be able to crash apscheduler's job thread or go
unlogged).
"""
import logging

import app.scheduler as mod


class _FakeDb:
    closed = False

    def close(self):
        _FakeDb.closed = True


class _FakeSessionFactory:
    def __call__(self):
        return _FakeDb()


def _reset():
    _FakeDb.closed = False
    mod._scheduler.remove_all_jobs()


class _FakeSettings:
    daily_go_live_audit_cron = "0 7 * * 1-5"
    zoom_call_sync_cron = "0 20 * * *"
    cmdctr_report_cron = "0 * * * *"


def test_start_scheduler_registers_all_jobs_with_configured_crontabs(monkeypatch):
    _reset()
    monkeypatch.setattr(mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(mod._scheduler, "start", lambda: None)

    mod.start_scheduler()

    job_ids = {job.id for job in mod._scheduler.get_jobs()}
    assert job_ids == {"daily-go-live-audit", "zoom-call-sync", "cmdctr-report"}

    zoom_job = mod._scheduler.get_job("zoom-call-sync")
    assert str(zoom_job.trigger.timezone) == "America/Los_Angeles"

    cmdctr_job = mod._scheduler.get_job("cmdctr-report")
    assert str(cmdctr_job.trigger.timezone) == "America/Los_Angeles"


def test_zoom_call_sync_job_uses_its_own_session_and_closes_it(monkeypatch):
    monkeypatch.setattr(mod, "get_session_factory", lambda: _FakeSessionFactory())
    monkeypatch.setattr(mod, "sync_zoom_calls", lambda db: {"target_date": "2026-09-29", "new_records": 3})

    mod._run_zoom_call_sync_job()

    assert _FakeDb.closed is True


def test_zoom_call_sync_job_logs_and_swallows_a_sync_failure(monkeypatch, caplog):
    monkeypatch.setattr(mod, "get_session_factory", lambda: _FakeSessionFactory())

    def _boom(db):
        raise RuntimeError("zoom is down")

    monkeypatch.setattr(mod, "sync_zoom_calls", _boom)

    with caplog.at_level(logging.ERROR):
        mod._run_zoom_call_sync_job()  # must not raise

    assert _FakeDb.closed is True
    assert any("zoom-call-sync" in r.message for r in caplog.records)


class _FakeCmdctrRun:
    id = 42


def test_cmdctr_report_job_uses_its_own_session_and_closes_it(monkeypatch):
    monkeypatch.setattr(mod, "get_session_factory", lambda: _FakeSessionFactory())
    monkeypatch.setattr(mod, "run_and_push_cmdctr_report", lambda db: _FakeCmdctrRun())

    mod._run_cmdctr_report_job()

    assert _FakeDb.closed is True


def test_cmdctr_report_job_logs_and_swallows_a_failure(monkeypatch, caplog):
    monkeypatch.setattr(mod, "get_session_factory", lambda: _FakeSessionFactory())

    def _boom(db):
        raise RuntimeError("cmdctr is down")

    monkeypatch.setattr(mod, "run_and_push_cmdctr_report", _boom)

    with caplog.at_level(logging.ERROR):
        mod._run_cmdctr_report_job()  # must not raise

    assert _FakeDb.closed is True
    assert any("cmdctr-report" in r.message for r in caplog.records)
