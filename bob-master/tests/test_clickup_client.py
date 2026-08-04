"""
Tests _RetryOn429Transport directly -- the real bug this guards against
(2026-08-04): a smoke test against real Atlas accounts hit ClickUp's 100
req/min rate limit within a handful of accounts once folder->lists->tasks->
comments fan-out was added, and every 429 was a hard failure with no retry.
"""
import time

import httpx

from app.integrations.clickup import _RetryOn429Transport


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
