"""
Google Sheets/Drive client for the heartbeat sheets. Mirrors the two-method
fallback in SKILL.md's TOOL QUIRK note: try the Sheets values.get read first,
fall back to exporting the file as CSV via the Drive API if that comes back
empty. Only report a sheet unreadable if both methods fail.

Setup requirement, not yet done anywhere: the service account (GOOGLE_SERVICE_ACCOUNT_JSON_B64)
must be individually shared (as Viewer) on each of the heartbeat/TV-feed sheets —
service accounts don't inherit the human bob@advancedmarketers.co's existing access.

GOOGLE_SERVICE_ACCOUNT_JSON_B64 holds the service-account JSON key, base64-encoded
into a single line — not a file path (no pre-populated local file in a Railway
deploy) and not raw JSON (Railway's variable editor has been observed truncating/
mangling a pasted multi-line JSON blob with embedded quotes and \n escapes).
Base64 sidesteps that: it's plain alphanumeric text, nothing for a web form's
input widget to misinterpret.

Generate it locally with:
    base64 -i service-account.json | tr -d '\n' | pbcopy   # macOS, copies to clipboard
    base64 -w0 service-account.json                         # Linux
"""
import base64
import csv
import io
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.config import get_settings

_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

# The "Checked at" column in the heartbeat sheets is a naive timestamp (no
# timezone marker) written by a script on the agency's own infrastructure
# (Orange County HQ, per docs/PROJECT-BRIEF-FOR-NEW-DEV.md). Assumed Pacific
# until confirmed otherwise — comparing it directly against UTC without this
# conversion is what made every single row read as "stale" on the first real
# run (a ~7-8h PDT/UTC gap reliably exceeds the 3h threshold).
_SHEET_TIMEZONE = ZoneInfo("America/Los_Angeles")


class GoogleDriveClient:
    def __init__(self) -> None:
        settings = get_settings()
        info = json.loads(base64.b64decode(settings.google_service_account_json_b64))
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=_SCOPES
        )
        self._sheets = build("sheets", "v4", credentials=credentials)
        self._drive = build("drive", "v3", credentials=credentials)

    def list_tabs(self, file_id: str) -> list[str]:
        spreadsheet = self._sheets.spreadsheets().get(spreadsheetId=file_id).execute()
        return [sheet["properties"]["title"] for sheet in spreadsheet.get("sheets", [])]

    def read_sheet_values(self, file_id: str, tab_name: str) -> list[list[str]]:
        try:
            resp = (
                self._sheets.spreadsheets()
                .values()
                .get(spreadsheetId=file_id, range=f"'{tab_name}'!A:Z")
                .execute()
            )
            values = resp.get("values", [])
            if values:
                return values
        except Exception:
            pass  # fall through to CSV export fallback below

        return self._read_sheet_csv_fallback(file_id)

    def _read_sheet_csv_fallback(self, file_id: str) -> list[list[str]]:
        request = self._drive.files().export_media(fileId=file_id, mimeType="text/csv")
        buffer = io.BytesIO(request.execute())
        text = buffer.getvalue().decode("utf-8")
        return list(csv.reader(io.StringIO(text)))

    @staticmethod
    def is_stale(checked_at: datetime) -> bool:
        """FRESHNESS CHECK, corrected per GoLive_Audit_Dev_Handover_Brief.md §1:
        the original audit checks the row's own "Checked at" is from *today*
        (same Pacific calendar day), not a rolling N-hour window. The Google
        Ads script refreshes on a staggered hourly cycle — some rows check in
        at 10:27, others at 13:27, both normal — so a fixed-hours threshold
        misclassifies legitimately-fresh-but-earlier-in-the-day rows as stale.
        This was the original design intent; a prior version of this function
        used a 3-hour window instead, which was a guess, not sourced.

        checked_at is naive — assumed to be _SHEET_TIMEZONE, not UTC. See that
        constant's comment before changing this."""
        if checked_at == datetime.min:
            return True  # unparseable/missing timestamp — treat as stale, not fresh
        return checked_at.date() != datetime.now(_SHEET_TIMEZONE).date()
