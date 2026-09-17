"""
Tests app.tasks.zoom_call_sync (sync_zoom_calls + backfill_zoom_calls)
against a fake ZoomClient + fake AtlasClient and a real (temp file) SQLite
DB -- proves dedup by meeting_uuid, the no-transcript skip, topic-to-account
matching (and its confidence floor), per-user/per-transcript soft-fail
behavior, and backfill_zoom_calls' chunking (Zoom silently clamps a
too-wide date range rather than erroring, confirmed against the real API --
see zoom_call_sync.py's _MAX_RANGE_DAYS).
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.tasks.zoom_call_sync as mod
from app.db import Base
from app.models import ZoomCallRecord


class _FakeZoomClient:
    users: list = []
    # {email: {(from_date, to_date): [recordings]}} -- a call only returns
    # recordings registered under the EXACT (from_date, to_date) pair it was
    # made with, so tests can prove backfill_zoom_calls actually issues the
    # right per-chunk ranges rather than one wide (silently-clampable) call.
    recordings_by_email_and_range: dict = {}
    transcripts_by_url: dict = {}
    fail_recordings_for_email: str | None = None
    fail_transcript_for_url: str | None = None
    recording_calls: list = []

    def list_users(self):
        return _FakeZoomClient.users

    def list_recordings_for_user(self, email, from_date, to_date):
        _FakeZoomClient.recording_calls.append((email, from_date, to_date))
        if email == _FakeZoomClient.fail_recordings_for_email:
            raise RuntimeError("zoom recordings error")
        return _FakeZoomClient.recordings_by_email_and_range.get(email, {}).get((from_date, to_date), [])

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
    _FakeZoomClient.recordings_by_email_and_range = {}
    _FakeZoomClient.transcripts_by_url = {}
    _FakeZoomClient.fail_recordings_for_email = None
    _FakeZoomClient.fail_transcript_for_url = None
    _FakeZoomClient.recording_calls = []
    _FakeAtlasClient.accounts = []


def _register(email, from_date, to_date, recordings):
    _FakeZoomClient.recordings_by_email_and_range.setdefault(email, {})[(from_date, to_date)] = recordings


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
    _register("tim@x.com", "2026-09-16", "2026-09-16",
               [_recording("uuid-1", "AM x Acme Co | Weekly Meeting", transcript_url="https://z/dl/1")])
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
    _register("tim@x.com", "2026-09-16", "2026-09-16",
               [_recording("uuid-1", "Tim's Personal Meeting Room", transcript_url=None)])

    db = _db(tmp_path)
    result = mod.sync_zoom_calls(db, target_date=date(2026, 9, 16))

    assert result["new_records"] == 0
    assert result["skipped_no_transcript"] == 1
    assert db.query(ZoomCallRecord).count() == 0


def test_already_stored_meeting_uuid_is_not_reprocessed(monkeypatch, tmp_path):
    _setup(monkeypatch)
    _FakeZoomClient.users = [{"email": "tim@x.com"}]
    _register("tim@x.com", "2026-09-16", "2026-09-16",
               [_recording("uuid-1", "AM x Acme Co", transcript_url="https://z/dl/1")])
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
    _register("tim@x.com", "2026-09-16", "2026-09-16",
               [_recording("uuid-1", "Mariachi Corazon de Maria", transcript_url="https://z/dl/1")])
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
    _register("tim@x.com", "2026-09-16", "2026-09-16",
               [_recording("uuid-1", "AM x Acme Co", transcript_url="https://z/dl/1")])
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
    _register("tim@x.com", "2026-09-16", "2026-09-16",
               [_recording("uuid-1", "AM x Acme Co", transcript_url="https://z/dl/1")])

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
    _register("tim@x.com", "2026-09-16", "2026-09-16",
               [_recording("uuid-1", "AM x Acme Co", transcript_url="https://z/dl/1")])
    _FakeZoomClient.transcripts_by_url = {"https://z/dl/1": "WEBVTT\n\nhello"}

    db = _db(tmp_path)
    result = mod.sync_zoom_calls(db, target_date=date(2026, 9, 16))

    assert result["matched"] == 0
    assert db.query(ZoomCallRecord).first().atlas_account_id is None


# --- backfill_zoom_calls -----------------------------------------------------

def test_backfill_chunks_a_wide_range_under_the_zoom_clamp_limit(monkeypatch, tmp_path):
    """A 45-day backfill must NOT be issued as one 45-day call -- Zoom
    silently clamps anything wider than ~1 month (confirmed against the
    real API, see _MAX_RANGE_DAYS), so a single wide call would silently
    lose the older portion. Proves two chunked calls happen instead, with
    no gap or overlap between them."""
    _setup(monkeypatch)
    _FakeZoomClient.users = [{"email": "tim@x.com"}]

    db = _db(tmp_path)
    mod.backfill_zoom_calls(db, days=45)

    calls = [c for c in _FakeZoomClient.recording_calls if c[0] == "tim@x.com"]
    assert len(calls) == 2
    (_, first_from, first_to), (_, second_from, second_to) = calls

    first_from_d = date.fromisoformat(first_from)
    first_to_d = date.fromisoformat(first_to)
    second_from_d = date.fromisoformat(second_from)
    second_to_d = date.fromisoformat(second_to)

    assert (first_to_d - first_from_d).days < mod._MAX_RANGE_DAYS
    assert (second_to_d - second_from_d).days < mod._MAX_RANGE_DAYS
    assert second_from_d == first_to_d + timedelta(days=1)  # contiguous, no gap or overlap


def test_backfill_finds_a_transcript_that_a_single_wide_call_would_have_missed(monkeypatch, tmp_path):
    """The real-world failure mode this whole feature exists to prevent:
    a recording old enough to fall in the FIRST chunk only shows up because
    backfill_zoom_calls actually issues that chunk's exact range -- a naive
    single 45-day call (which Zoom would clamp to its last ~30 days) would
    never have surfaced it at all."""
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [{"id": "acme-1", "companyName": "Acme Co", "isActive": True}]
    _FakeZoomClient.users = [{"email": "tim@x.com"}]

    today = datetime.now(timezone.utc).date()
    overall_start = today - timedelta(days=45)
    chunk1_end = overall_start + timedelta(days=mod._MAX_RANGE_DAYS - 1)

    _register("tim@x.com", overall_start.isoformat(), chunk1_end.isoformat(),
               [_recording("old-uuid", "AM x Acme Co", start_time=f"{overall_start.isoformat()}T10:00:00Z",
                           transcript_url="https://z/dl/old")])
    _FakeZoomClient.transcripts_by_url = {"https://z/dl/old": "WEBVTT\n\nold call"}

    db = _db(tmp_path)
    result = mod.backfill_zoom_calls(db, days=45)

    assert result["new_records"] == 1
    assert result["chunks"] == 2
    row = db.query(ZoomCallRecord).filter_by(meeting_uuid="old-uuid").first()
    assert row is not None
    assert row.atlas_account_id == "acme-1"


def test_backfill_does_not_reprocess_a_uuid_seen_in_an_earlier_chunk(monkeypatch, tmp_path):
    _setup(monkeypatch)
    _FakeZoomClient.users = [{"email": "tim@x.com"}]

    today = datetime.now(timezone.utc).date()
    overall_start = today - timedelta(days=45)
    chunk1_end = overall_start + timedelta(days=mod._MAX_RANGE_DAYS - 1)
    chunk2_start = chunk1_end + timedelta(days=1)
    chunk2_end = today - timedelta(days=1)

    rec = _recording("dupe-uuid", "AM x Acme Co", transcript_url="https://z/dl/1")
    # Same uuid registered as if Zoom returned it in both chunk windows
    # (recurring/PMI meetings can do this) -- must only be stored once.
    _register("tim@x.com", overall_start.isoformat(), chunk1_end.isoformat(), [rec])
    _register("tim@x.com", chunk2_start.isoformat(), chunk2_end.isoformat(), [rec])
    _FakeZoomClient.transcripts_by_url = {"https://z/dl/1": "WEBVTT\n\nhello"}

    db = _db(tmp_path)
    result = mod.backfill_zoom_calls(db, days=45)

    assert result["new_records"] == 1
    assert db.query(ZoomCallRecord).count() == 1


def test_backfill_aggregates_stats_and_errors_across_chunks(monkeypatch, tmp_path):
    _setup(monkeypatch)
    _FakeZoomClient.users = [{"email": "broken@x.com"}]
    _FakeZoomClient.fail_recordings_for_email = "broken@x.com"

    db = _db(tmp_path)
    result = mod.backfill_zoom_calls(db, days=45)

    assert result["chunks"] == 2
    assert len(result["user_errors"]) == 2  # one failure per chunk for this user
