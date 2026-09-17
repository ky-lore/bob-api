"""
Thin REST client for Zoom's account-wide APIs via a Server-to-Server OAuth
app (2026-09-17) -- deliberately NOT the personal-user Zoom MCP connector,
which can only ever see meetings the currently-authorized user personally
hosted or attended (confirmed the hard way: connected as a full Zoom admin,
still only surfaced one meeting, and it was a vendor's demo the admin sat in
on as an invitee -- see chat history). Account-level scopes on this app are
what actually reach every user's meetings/recordings/transcripts.

Auth is the account_credentials grant (HTTP Basic client_id/client_secret,
account_id as a query param) -- same short-lived-token-cached-in-memory
pattern as adspend/google_ads_client.py's refresh-token flow.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import get_settings

_TOKEN_URL = "https://zoom.us/oauth/token"
_API_HOST = "https://api.zoom.us/v2"


class ZoomClient:
    def __init__(self) -> None:
        settings = get_settings()
        if not (settings.zoom_account_id and settings.zoom_client_id and settings.zoom_client_secret):
            raise RuntimeError(
                "ZoomClient requires ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, and ZOOM_CLIENT_SECRET to be set"
            )
        self._settings = settings
        self._client = httpx.Client(timeout=30.0)
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        resp = self._client.post(
            _TOKEN_URL,
            params={"grant_type": "account_credentials", "account_id": self._settings.zoom_account_id},
            auth=(self._settings.zoom_client_id, self._settings.zoom_client_secret),
        )
        resp.raise_for_status()
        body = resp.json()
        self._access_token = body["access_token"]
        self._token_expires_at = time.time() + body.get("expires_in", 3600)
        return self._access_token

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._client.get(
            f"{_API_HOST}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._get_access_token()}"},
        )
        resp.raise_for_status()
        return resp.json()

    def list_users(self, page_size: int = 300) -> list[dict[str, Any]]:
        """Every user on the account -- id, email, first/last name. Paginates
        automatically via next_page_token."""
        users: list[dict[str, Any]] = []
        next_page_token = ""
        while True:
            params = {"page_size": page_size}
            if next_page_token:
                params["next_page_token"] = next_page_token
            body = self._get("/users", params=params)
            users.extend(body.get("users", []))
            next_page_token = body.get("next_page_token") or ""
            if not next_page_token:
                return users

    def list_user_meetings(self, user_id: str, from_date: str, to_date: str, page_size: int = 300) -> list[dict[str, Any]]:
        """Past meetings a user hosted in [from_date, to_date] (YYYY-MM-DD),
        via the Report API -- topic, start/end time, UUID for pulling
        recordings/transcripts after. Zoom caps this range at one month per
        call, same as the recordings endpoint."""
        meetings: list[dict[str, Any]] = []
        next_page_token = ""
        while True:
            params = {"from": from_date, "to": to_date, "page_size": page_size}
            if next_page_token:
                params["next_page_token"] = next_page_token
            body = self._get(f"/report/users/{user_id}/meetings", params=params)
            meetings.extend(body.get("meetings", []))
            next_page_token = body.get("next_page_token") or ""
            if not next_page_token:
                return meetings

    def list_recordings_for_user(self, user_id: str, from_date: str, to_date: str, page_size: int = 300) -> list[dict[str, Any]]:
        """Cloud recordings for a user in [from_date, to_date] (YYYY-MM-DD) --
        unlike list_user_meetings (every meeting, recorded or not), this only
        ever returns meetings that actually have a recording, each with a
        recording_files array (a TRANSCRIPT entry has the transcript
        download_url, when transcription was enabled)."""
        meetings: list[dict[str, Any]] = []
        next_page_token = ""
        while True:
            params = {"from": from_date, "to": to_date, "page_size": page_size}
            if next_page_token:
                params["next_page_token"] = next_page_token
            body = self._get(f"/users/{user_id}/recordings", params=params)
            meetings.extend(body.get("meetings", []))
            next_page_token = body.get("next_page_token") or ""
            if not next_page_token:
                return meetings

    def get_transcript_text(self, download_url: str) -> str:
        """Downloads a recording_files TRANSCRIPT entry's raw WEBVTT content
        (speaker-labeled, timestamped) -- confirmed against a real recording,
        2026-09-17: needs Bearer auth AND follow_redirects (the download_url
        302s once before serving the actual file)."""
        resp = self._client.get(
            download_url,
            headers={"Authorization": f"Bearer {self._get_access_token()}"},
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text
