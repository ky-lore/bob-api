"""
Thin REST client for the ClickUp surface daily-go-live-audit needs. ClickUp API v2
auth is the raw token in the Authorization header — no "Bearer " prefix.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


class ClickUpClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.clickup_base_url
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": settings.clickup_api_token, "Content-Type": "application/json"},
            timeout=30.0,
        )

    def get_list_tasks(self, list_id: str, *, include_closed: bool = False, page: int = 0) -> dict[str, Any]:
        resp = self._client.get(
            f"/list/{list_id}/task",
            params={"include_closed": str(include_closed).lower(), "page": page},
        )
        resp.raise_for_status()
        return resp.json()

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
