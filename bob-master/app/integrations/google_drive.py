"""
Google Sheets/Drive client for the heartbeat sheets. Mirrors the two-method
fallback in SKILL.md's TOOL QUIRK note: try the Sheets values.get read first,
fall back to exporting the file as CSV via the Drive API if that comes back
empty. Only report a sheet unreadable if both methods fail.

Setup requirement, not yet done anywhere: the service account (GOOGLE_SERVICE_ACCOUNT_JSON)
must be individually shared (as Viewer) on each of the heartbeat/TV-feed sheets —
service accounts don't inherit the human bob@advancedmarketers.co's existing access.
"""
import csv
import io
from datetime import datetime, timedelta
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.config import get_settings

_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]


class GoogleDriveClient:
    def __init__(self) -> None:
        settings = get_settings()
        credentials = service_account.Credentials.from_service_account_file(
            settings.google_service_account_json, scopes=_SCOPES
        )
        self._sheets = build("sheets", "v4", credentials=credentials)
        self._drive = build("drive", "v3", credentials=credentials)

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
    def is_stale(checked_at: datetime, *, max_age_hours: int = 3) -> bool:
        """FRESHNESS CHECK per SKILL.md: heartbeat sheets are refreshed hourly by
        separate scripts. If the sheet's own 'Checked at' timestamp is older than
        this, say so in the digest instead of trusting the numbers."""
        return datetime.utcnow() - checked_at > timedelta(hours=max_age_hours)
