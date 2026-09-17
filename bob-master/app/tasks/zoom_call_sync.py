"""
Daily pull of the previous day's Zoom call transcripts, stored in Postgres
(2026-09-17). Per user (there's no true account-wide "all meetings" endpoint
-- Zoom's Dashboard API would be, but it's paywalled behind a Business+ plan
this account doesn't have, see chat history): list that user's recordings
for the target day, skip anything already stored (by meeting_uuid), pull the
TRANSCRIPT file for anything new, fuzzy-match the topic against Atlas
company names, and store one ZoomCallRecord per new transcript.

Deliberately per-day, not incremental-since-last-sync: at this volume (26
users, one date-range call each) a full day's pull is cheap regardless, and
"yesterday" is a simpler, more obviously-correct unit to reason about and
re-run than a rolling watermark.

Meetings with a recording but no TRANSCRIPT file are skipped entirely --
nothing useful to store yet. Not wired into the scheduler yet; manually
triggered only until a real day's output has been reviewed (see
app/routers/zoom_call_sync.py), same rollout posture as atlas_campaign_push.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.atlas_client import AtlasClient
from app.integrations.zoom_client import ZoomClient
from app.models import ZoomCallRecord
from app.tasks.account_name_matching import best_match, normalize

# Same bar as the ClickUp folder re-bridge's "confirmed, no human review
# needed" tier (0.82) -- deliberately NOT its lower 0.55 "worth a human
# glance" tier, since nothing here surfaces a review list; whatever clears
# this gets written straight into matched_account_name and could get trusted
# downstream. Confirmed necessary against real data, 2026-09-17: an
# "Introduction" call topic (no real company name in it at all) scored 0.65
# against an unrelated company purely from short-string character overlap --
# below this bar, above the old 0.55 one. Real correct matches in the same
# smoke test all scored 0.92+, so there's real headroom here, not a
# hair-trigger cutoff.
_MIN_MATCH_CONFIDENCE = 0.82


def sync_zoom_calls(db: Session, target_date: date | None = None) -> dict[str, Any]:
    """Returns {"target_date", "new_records", "matched", "skipped_no_transcript",
    "user_errors": [{"email", "error"}, ...]}."""
    target_date = target_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    from_date = to_date = target_date.isoformat()

    zoom = ZoomClient()
    atlas_accounts = [a for a in AtlasClient().get_all_accounts() if a.get("isActive")]
    account_norms = {
        a["companyName"]: normalize(a["companyName"]) for a in atlas_accounts if a.get("companyName")
    }

    existing_uuids = {row[0] for row in db.query(ZoomCallRecord.meeting_uuid).all()}

    new_records = 0
    matched = 0
    skipped_no_transcript = 0
    user_errors: list[dict[str, str]] = []

    for user in zoom.list_users():
        email = user.get("email")
        if not email:
            continue
        try:
            recordings = zoom.list_recordings_for_user(email, from_date, to_date)
        except Exception as exc:
            user_errors.append({"email": email, "error": str(exc)})
            continue

        for rec in recordings:
            meeting_uuid = rec.get("uuid")
            if not meeting_uuid or meeting_uuid in existing_uuids:
                continue

            transcript_file = next(
                (f for f in rec.get("recording_files", []) if f.get("file_type") == "TRANSCRIPT"), None
            )
            if not transcript_file:
                skipped_no_transcript += 1
                continue

            try:
                transcript_text = zoom.get_transcript_text(transcript_file["download_url"])
            except Exception as exc:
                user_errors.append({"email": email, "error": f"transcript download failed ({meeting_uuid}): {exc}"})
                continue

            topic = rec.get("topic") or ""
            match_name, confidence = best_match(normalize(topic), account_norms)
            if match_name and confidence < _MIN_MATCH_CONFIDENCE:
                match_name = None
            if match_name:
                matched += 1

            db.add(ZoomCallRecord(
                meeting_uuid=meeting_uuid,
                host_email=email,
                topic=topic,
                start_time=datetime.fromisoformat(rec["start_time"].replace("Z", "+00:00")),
                matched_account_name=match_name,
                match_confidence=confidence if match_name else None,
                transcript_text=transcript_text,
                pulled_at=datetime.now(timezone.utc),
            ))
            existing_uuids.add(meeting_uuid)
            new_records += 1

    db.commit()
    return {
        "target_date": target_date.isoformat(),
        "new_records": new_records,
        "matched": matched,
        "skipped_no_transcript": skipped_no_transcript,
        "user_errors": user_errors,
    }
