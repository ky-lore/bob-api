"""
Tests _RetryOn429Transport directly -- the real bug this guards against
(2026-08-04): a smoke test against real Atlas accounts hit ClickUp's 100
req/min rate limit within a handful of accounts once folder->lists->tasks->
comments fan-out was added, and every 429 was a hard failure with no retry.
"""
import time

import httpx
import pytest

from app.integrations.clickup import ClickUpClient, _RetryOn429Transport


def test_retries_on_429_using_the_x_ratelimit_reset_header(monkeypatch):
    calls = {"n": 0}

    def fake_handle_request(self, request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"x-ratelimit-reset": str(int(time.time()) + 1)}, request=request)
        return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle_request)
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    transport = _RetryOn429Transport()
    request = httpx.Request("GET", "https://api.clickup.com/api/v2/test")
    response = transport.handle_request(request)

    assert response.status_code == 200
    assert calls["n"] == 2
    assert len(sleep_calls) == 1


def test_falls_back_to_exponential_backoff_without_the_header(monkeypatch):
    calls = {"n": 0}

    def fake_handle_request(self, request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, request=request)  # no x-ratelimit-reset header at all
        return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle_request)
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    transport = _RetryOn429Transport()
    request = httpx.Request("GET", "https://api.clickup.com/api/v2/test")
    response = transport.handle_request(request)

    assert response.status_code == 200
    assert sleep_calls == [1.0]  # 2.0**0 on the first retry


def test_gives_up_after_max_retries_and_returns_the_429(monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request: httpx.Response(429, request=request))
    monkeypatch.setattr(time, "sleep", lambda s: None)

    transport = _RetryOn429Transport(max_retries=2)
    request = httpx.Request("GET", "https://api.clickup.com/api/v2/test")
    response = transport.handle_request(request)

    assert response.status_code == 429


def test_does_not_retry_on_success(monkeypatch):
    calls = {"n": 0}

    def fake_handle_request(self, request):
        calls["n"] += 1
        return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle_request)
    transport = _RetryOn429Transport()
    request = httpx.Request("GET", "https://api.clickup.com/api/v2/test")
    response = transport.handle_request(request)

    assert response.status_code == 200
    assert calls["n"] == 1


def _client(monkeypatch) -> ClickUpClient:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ATLAS_API_KEY", "x")
    from app.config import get_settings

    get_settings.cache_clear()
    return ClickUpClient()


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _FakeHttpClient:
    """Replaces ClickUpClient._client after construction -- returns queued
    responses in order and records every (url, params) call for assertions,
    same convention as test_slack_client.py's _FakeWebClient."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return self._responses.pop(0)


def _task(task_id, tag="action"):
    return {"id": task_id, "name": f"task {task_id}", "tags": [{"name": tag}]}


def test_get_team_tasks_by_tag_uses_the_configured_workspace_id(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpClient([_FakeResponse({"tasks": [_task("1")], "last_page": True})])
    client._client = fake

    tasks = client.get_team_tasks_by_tag("action")

    assert [t["id"] for t in tasks] == ["1"]
    url, params = fake.calls[0]
    assert url == f"/team/{client._workspace_id}/task"
    assert params == {"tags[]": "action", "page": 0}


def test_get_team_tasks_by_tag_paginates_to_completion(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeHttpClient([
        _FakeResponse({"tasks": [_task("1"), _task("2")], "last_page": False}),
        _FakeResponse({"tasks": [_task("3")], "last_page": True}),
    ])
    client._client = fake

    tasks = client.get_team_tasks_by_tag("action")

    assert [t["id"] for t in tasks] == ["1", "2", "3"]
    assert [params["page"] for _, params in fake.calls] == [0, 1]


def test_get_team_tasks_by_tag_stops_on_an_empty_page_even_if_last_page_is_missing(monkeypatch):
    # Defensive: don't loop forever if a response is missing last_page entirely.
    client = _client(monkeypatch)
    fake = _FakeHttpClient([_FakeResponse({"tasks": []})])
    client._client = fake

    tasks = client.get_team_tasks_by_tag("action")

    assert tasks == []
    assert len(fake.calls) == 1
