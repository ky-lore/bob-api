"""
Tests app.tasks.zoom_call_sync.sync_zoom_calls against a fake ZoomClient +
fake AtlasClient and a real (temp file) SQLite DB -- proves dedup by
meeting_uuid, the no-transcript skip, topic-to-account matching (and its
confidence floor), and per-user/per-transcript soft-fail behavior.
"""
from datetime import date, datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.tasks.zoom_call_sync as mod
from app.db import Base
from app.models import ZoomCallRecord


class _FakeZoomClient:
    users: list = []
    recordings_by_email: dict = {}
    transcripts_by_url: dict = {}
    fail_recordings_for_email: str | None = None
    fail_transcript_for_url: str | None = None

    def list_users(self):
        return _FakeZoomClient.users

    def list_recordings_for_user(self, email, from_date, to_date):
        if email == _FakeZoomClient.fail_recordings_for_email:
            raise RuntimeError("zoom recordings error")
        return _FakeZoomClient.recordings_by_email.get(email, [])

    def get_transcript_text(self, download_url):
        if download_url == _FakeZoomClient.fail_transcript_for_url:
            raise RuntimeError("download failed")
        return _FakeZoomClient.transcripts_by_url[download_url]


class _FakeAtlasClient:
    accounts: list = []

    def get_all_accounts(self):
        return _FakeAtlasClient.accounts


def _reset():
    _FakeZoomClient.users = []
    _FakeZoomClient.recordings_by_email = {}
    _FakeZoomClient.transcripts_by_url = {}
    _FakeZoomClient.fail_recordings_for_email = None
    _FakeZoomClient.fail_transcript_for_url = None
    _FakeAtlasClient.accounts = []


def _recording(uuid, topic, start_time="2026-09-16T20:00:00Z", transcript_url=None):
    files = []
    if transcript_url:
        files.append({"file_type": "TRANSCRIPT", "download_url": transcript_url})
    return {"uuid": uuid, "topic": topic, "start_time": start_time, "recording_files": files}


def _db(tmp_path, name="zoom_sync.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _setup(monkeypatch):
    _reset()
    monkeypatch.setattr(mod, "ZoomClient", _FakeZoomClient)
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)


def test_new_transcript_is_stored_and_matched_to_atlas_account(monkeypatch, tmp_path):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [{"id": "acme-1", "companyName": "Acme Co", "isActive": True}]
    _FakeZoomClient.users = [{"email": "tim@x.com"}]
    _FakeZoomClient.recordings_by_email = {
        "tim@x.com": [_recording("uuid-1", "AM x Acme Co | Weekly Meeting", transcript_url="https://z/dl/1")],
    }
    _FakeZoomClient.transcripts_by_url = {"https://z/dl/1": "WEBVTT\n\nhello"}

    db = _db(tmp_path)
    result = mod.sync_zoom_calls(db, target_date=date(2026, 9, 16))

    assert result["new_records"] == 1
    assert result["matched"] == 1
    assert result["user_errors"] == []

    row = db.query(ZoomCallRecord).filter_by(meeting_uuid="uuid-1").first()
    assert row.atlas_account_id == "acme-1"
    assert row.matched_company_name == "Acme Co"
    assert row.transcript_text == "WEBVTT\n\nhello"
    assert row.host_email == "tim@x.com"


def test_meeting_without_a_transcript_file_is_skipped_not_stored(monkeypatch, tmp_path):
    _setup(monkeypatch)
    _FakeZoomClient.users = [{"email": "tim@x.com"}]
    _FakeZoomClient.recordings_by_email = {
        "tim@x.com": [_recording("uuid-1", "Tim's Personal Meeting Room", transcript_url=None)],
    }

    db = _db(tmp_path)
    result = mod.sync_zoom_calls(db, target_date=date(2026, 9, 16))

    assert result["new_records"] == 0
    assert result["skipped_no_transcript"] == 1
    assert db.query(ZoomCallRecord).count() == 0


def test_already_stored_meeting_uuid_is_not_reprocessed(monkeypatch, tmp_path):
    _setup(monkeypatch)
    _FakeZoomClient.users = [{"email": "tim@x.com"}]
    _FakeZoomClient.recordings_by_email = {
        "tim@x.com": [_recording("uuid-1", "AM x Acme Co", transcript_url="https://z/dl/1")],
    }
    _FakeZoomClient.transcripts_by_url = {"https://z/dl/1": "WEBVTT\n\nhello"}

    db = _db(tmp_path)
    db.add(ZoomCallRecord(
        meeting_uuid="uuid-1", host_email="tim@x.com", topic="AM x Acme Co",
        start_time=datetime(2026, 9, 16, 20, 0, tzinfo=timezone.utc),
        atlas_account_id=None, matched_company_name=None, match_confidence=None, transcript_text="already here",
        pulled_at=datetime.now(timezone.utc),
    ))
    db.commit()

    result = mod.sync_zoom_calls(db, target_date=date(2026, 9, 16))

    assert result["new_records"] == 0
    assert db.query(ZoomCallRecord).count() == 1
    assert db.query(ZoomCallRecord).first().transcript_text == "already here"  # untouched, not re-fetched


def test_low_confidence_match_is_not_stored_as_a_real_match(monkeypatch, tmp_path):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [{"id": "df-1", "companyName": "Drain Force Plumbing", "isActive": True}]
    _FakeZoomClient.users = [{"email": "tim@x.com"}]
    _FakeZoomClient.recordings_by_email = {
        "tim@x.com": [_recording("uuid-1", "Mariachi Corazon de Maria", transcript_url="https://z/dl/1")],
    }
    _FakeZoomClient.transcripts_by_url = {"https://z/dl/1": "WEBVTT\n\nhello"}

    db = _db(tmp_path)
    result = mod.sync_zoom_calls(db, target_date=date(2026, 9, 16))

    assert result["new_records"] == 1
    assert result["matched"] == 0
    row = db.query(ZoomCallRecord).first()
    assert row.atlas_account_id is None
    assert row.matched_company_name is None
    assert row.match_confidence is None


def test_recordings_pull_failure_for_one_user_does_not_block_others(monkeypatch, tmp_path):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [{"id": "acme-1", "companyName": "Acme Co", "isActive": True}]
    _FakeZoomClient.users = [{"email": "broken@x.com"}, {"email": "tim@x.com"}]
    _FakeZoomClient.fail_recordings_for_email = "broken@x.com"
    _FakeZoomClient.recordings_by_email = {
        "tim@x.com": [_recording("uuid-1", "AM x Acme Co", transcript_url="https://z/dl/1")],
    }
    _FakeZoomClient.transcripts_by_url = {"https://z/dl/1": "WEBVTT\n\nhello"}

    db = _db(tmp_path)
    result = mod.sync_zoom_calls(db, target_date=date(2026, 9, 16))

    assert result["new_records"] == 1
    assert len(result["user_errors"]) == 1
    assert result["user_errors"][0]["email"] == "broken@x.com"


def test_transcript_download_failure_is_soft_failed(monkeypatch, tmp_path):
    _setup(monkeypatch)
    _FakeZoomClient.users = [{"email": "tim@x.com"}]
    _FakeZoomClient.fail_transcript_for_url = "https://z/dl/1"
    _FakeZoomClient.recordings_by_email = {
        "tim@x.com": [_recording("uuid-1", "AM x Acme Co", transcript_url="https://z/dl/1")],
    }

    db = _db(tmp_path)
    result = mod.sync_zoom_calls(db, target_date=date(2026, 9, 16))

    assert result["new_records"] == 0
    assert len(result["user_errors"]) == 1
    assert "transcript download failed" in result["user_errors"][0]["error"]
    assert db.query(ZoomCallRecord).count() == 0


def test_inactive_atlas_accounts_are_excluded_from_matching(monkeypatch, tmp_path):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [{"id": "acme-1", "companyName": "Acme Co", "isActive": False}]
    _FakeZoomClient.users = [{"email": "tim@x.com"}]
    _FakeZoomClient.recordings_by_email = {
        "tim@x.com": [_recording("uuid-1", "AM x Acme Co", transcript_url="https://z/dl/1")],
    }
    _FakeZoomClient.transcripts_by_url = {"https://z/dl/1": "WEBVTT\n\nhello"}

    db = _db(tmp_path)
    result = mod.sync_zoom_calls(db, target_date=date(2026, 9, 16))

    assert result["matched"] == 0
    assert db.query(ZoomCallRecord).first().atlas_account_id is None
