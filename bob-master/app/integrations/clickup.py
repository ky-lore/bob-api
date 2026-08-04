"""
Thin REST client for the ClickUp surface daily-go-live-audit needs. ClickUp API v2
auth is the raw token in the Authorization header — no "Bearer " prefix.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import get_settings


class _RetryOn429Transport(httpx.HTTPTransport):
    """ClickUp's rate limit (100 req/min per token, confirmed via real
    x-ratelimit-* response headers, 2026-08-04) is easy to hit once a single
    account's context gather fans out to folder -> lists -> tasks -> comments
    (real example: ~90 comment-fetch calls for one account), let alone
    looping that across Atlas's 133 accounts. Retries on 429, waiting until
    x-ratelimit-reset (a unix-epoch second ClickUp actually sends) rather
    than guessing a backoff interval — confirmed ClickUp does NOT send a
    Retry-After header the way Slack does."""

    def __init__(self, *args: Any, max_retries: int = 3, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._max_retries = max_retries

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            response = super().handle_request(request)
            if response.status_code != 429 or attempt == self._max_retries:
                return response
            response.close()
            reset_at = response.headers.get("x-ratelimit-reset")
            wait_seconds = max(1.0, float(reset_at) - time.time() + 1) if reset_at else 2.0**attempt
            time.sleep(min(wait_seconds, 65.0))
        return response


class ClickUpClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.clickup_base_url
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": settings.clickup_api_token, "Content-Type": "application/json"},
            timeout=30.0,
            transport=_RetryOn429Transport(),
        )

    def get_list_tasks(self, list_id: str, *, include_closed: bool = False, page: int = 0) -> dict[str, Any]:
        resp = self._client.get(
            f"/list/{list_id}/task",
            params={"include_closed": str(include_closed).lower(), "page": page},
        )
        resp.raise_for_status()
        return resp.json()

    def get_folder_lists(self, folder_id: str) -> list[dict[str, Any]]:
        """Lists inside a Folder (Space > Folder > List > Task) — used for
        Atlas's per-client clickupFolderId, which is a real Folder, not a
        List. Confirmed shape against a real folder 2026-08-04: {"lists": [...]}."""
        resp = self._client.get(f"/folder/{folder_id}/list")
        resp.raise_for_status()
        return resp.json().get("lists", [])

    def get_task(self, task_id: str) -> dict[str, Any]:
        resp = self._client.get(f"/task/{task_id}")
        resp.raise_for_status()
        return resp.json()

    def get_task_with_subtasks(self, task_id: str) -> dict[str, Any]:
        """Same as get_task, but the response's "subtasks" key is populated
        with full subtask objects (ClickUp normally omits subtasks unless
        asked) — used for pulling every [CLIENT]/[AM] blocker subtask under a
        go-live card."""
        resp = self._client.get(f"/task/{task_id}", params={"include_subtasks": "true"})
        resp.raise_for_status()
        return resp.json()

    def get_task_comments(self, task_id: str) -> list[dict[str, Any]]:
        resp = self._client.get(f"/task/{task_id}/comment")
        resp.raise_for_status()
        return resp.json().get("comments", [])

    def update_task_status(self, task_id: str, status: str) -> dict[str, Any]:
        resp = self._client.put(f"/task/{task_id}", json={"status": status})
        resp.raise_for_status()
        return resp.json()

    def add_tag_to_task(self, task_id: str, tag_name: str) -> None:
        resp = self._client.post(f"/task/{task_id}/tag/{tag_name}")
        resp.raise_for_status()

    def add_comment(self, task_id: str, comment_text: str) -> dict[str, Any]:
        resp = self._client.post(f"/task/{task_id}/comment", json={"comment_text": comment_text})
        resp.raise_for_status()
        return resp.json()

    def create_task(self, list_id: str, name: str, *, description: str = "", tags: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name, "description": description}
        if tags:
            payload["tags"] = tags
        resp = self._client.post(f"/list/{list_id}/task", json=payload)
        resp.raise_for_status()
        return resp.json()

    def create_subtask(self, parent_task_id: str, name: str, *, list_id: str) -> dict[str, Any]:
        resp = self._client.post(
            f"/list/{list_id}/task",
            json={"name": name, "parent": parent_task_id},
        )
        resp.raise_for_status()
        return resp.json()

    def get_bulk_time_in_status(self, task_ids: list[str]) -> dict[str, Any]:
        resp = self._client.get("/task/bulk_time_in_status/task_ids", params={"task_ids": task_ids})
        resp.raise_for_status()
        return resp.json()
