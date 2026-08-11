"""
Tests app.integrations.anthropic_client's batching logic directly (mocking
_synthesize_batch, not the real API) — the behavior that matters here is how
multiple batches combine and degrade, not the Claude call itself. A separate
group of tests below exercises _coerce_list / the two _synthesize_*_batch
functions directly, against a faked Anthropic client, to cover the real
double-encoded-array response quirk found 2026-08-06.
"""
import json
import types

from app.integrations import anthropic_client


def _accounts(n: int) -> list[dict]:
    return [{"account": f"Account {i}", "day": i, "stage": "onboarding", "context": [f"flag {i}"]} for i in range(n)]


def test_synthesize_account_narratives_splits_into_batches(monkeypatch):
    calls = []

    def _fake_batch(batch):
        calls.append(len(batch))
        return {a["account"]: f"narrative for {a['account']}" for a in batch}

    monkeypatch.setattr(anthropic_client, "_synthesize_batch", _fake_batch)
    monkeypatch.setattr(anthropic_client, "_BATCH_SIZE", 20)

    result, batch_results = anthropic_client.synthesize_account_narratives(_accounts(45))

    assert calls == [20, 20, 5]
    assert len(result) == 45
    assert result["Account 0"] == "narrative for Account 0"

    assert [b["ok"] for b in batch_results] == [True, True, True]
    assert [len(b["accounts"]) for b in batch_results] == [20, 20, 5]
    assert [b["narrated_count"] for b in batch_results] == [20, 20, 5]


def test_synthesize_account_narratives_partial_batch_failure_keeps_successful_ones(monkeypatch):
    def _fake_batch(batch):
        if batch[0]["account"] == "Account 20":
            raise RuntimeError("stop_reason=max_tokens")
        return {a["account"]: "ok" for a in batch}

    monkeypatch.setattr(anthropic_client, "_synthesize_batch", _fake_batch)
    monkeypatch.setattr(anthropic_client, "_BATCH_SIZE", 20)

    result, batch_results = anthropic_client.synthesize_account_narratives(_accounts(45))

    # First batch (0-19) and third batch (40-44) succeeded; second batch (20-39) failed.
    assert "Account 0" in result
    assert "Account 44" in result
    assert "Account 20" not in result
    assert len(result) == 25

    assert [b["ok"] for b in batch_results] == [True, False, True]
    assert "stop_reason=max_tokens" in batch_results[1]["error"]
    assert batch_results[1]["accounts"][0] == "Account 20"
    assert batch_results[1]["narrated_count"] == 0


def test_synthesize_account_narratives_raises_only_if_every_batch_fails(monkeypatch):
    def _always_fail(batch):
        raise RuntimeError("boom")

    monkeypatch.setattr(anthropic_client, "_synthesize_batch", _always_fail)
    monkeypatch.setattr(anthropic_client, "_BATCH_SIZE", 20)

    try:
        anthropic_client.synthesize_account_narratives(_accounts(25))
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "boom" in str(exc)


def test_synthesize_account_narratives_empty_input_returns_empty_without_calling_batch(monkeypatch):
    def _should_not_be_called(batch):
        raise AssertionError("should not be called for empty input")

    monkeypatch.setattr(anthropic_client, "_synthesize_batch", _should_not_be_called)
    assert anthropic_client.synthesize_account_narratives([]) == ({}, [])


def test_synthesize_account_reports_returns_status_and_recent_work_per_account(monkeypatch):
    def _fake_report_batch(batch):
        return {a["account"]: {"status": f"status {a['account']}", "recent_work": f"work {a['account']}"} for a in batch}

    monkeypatch.setattr(anthropic_client, "_synthesize_report_batch", _fake_report_batch)
    monkeypatch.setattr(anthropic_client, "_BATCH_SIZE", 20)

    result, batch_results = anthropic_client.synthesize_account_reports(_accounts(5))

    assert result["Account 0"] == {"status": "status Account 0", "recent_work": "work Account 0"}
    assert len(result) == 5
    assert batch_results[0]["ok"] is True


def test_synthesize_account_reports_shares_the_same_partial_failure_batching(monkeypatch):
    def _fake_report_batch(batch):
        if batch[0]["account"] == "Account 20":
            raise RuntimeError("boom")
        return {a["account"]: {"status": "ok", "recent_work": "ok"} for a in batch}

    monkeypatch.setattr(anthropic_client, "_synthesize_report_batch", _fake_report_batch)
    monkeypatch.setattr(anthropic_client, "_BATCH_SIZE", 20)

    result, batch_results = anthropic_client.synthesize_account_reports(_accounts(45))

    assert "Account 0" in result
    assert "Account 20" not in result
    assert [b["ok"] for b in batch_results] == [True, False, True]


class _FakeSettings:
    anthropic_api_key = "x"
    anthropic_model = "claude-sonnet-5"


def _fake_tool_response(tool_name, input_):
    block = types.SimpleNamespace(type="tool_use", name=tool_name, input=input_)
    return types.SimpleNamespace(content=[block], stop_reason="tool_use")


def _patch_anthropic(monkeypatch, response):
    monkeypatch.setattr(anthropic_client, "get_settings", lambda: _FakeSettings())
    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=lambda **kwargs: response))
    monkeypatch.setattr(anthropic_client.anthropic, "Anthropic", lambda api_key: fake_client)


def test_coerce_list_parses_a_json_encoded_string_back_into_a_list():
    assert anthropic_client._coerce_list('[{"a": 1}]') == [{"a": 1}]


def test_coerce_list_unwraps_a_self_nested_stringified_object(monkeypatch):
    # Real bug, 2026-08-06: {"reports": "{\"reports\": [...]}"} -- the whole
    # object stringified and nested one level inside its own key.
    nested = json.dumps({"reports": [{"a": 1}]})
    assert anthropic_client._coerce_list(nested, key="reports") == [{"a": 1}]


def test_coerce_list_returns_empty_for_garbage_or_wrong_type():
    assert anthropic_client._coerce_list("not json") == []
    assert anthropic_client._coerce_list(42) == []
    assert anthropic_client._coerce_list(None) == []


def test_coerce_list_passes_through_a_real_list_unchanged():
    assert anthropic_client._coerce_list([{"a": 1}]) == [{"a": 1}]


def test_synthesize_batch_handles_narratives_double_encoded_as_a_json_string(monkeypatch):
    # Real bug, 2026-08-06: Claude occasionally returns {"narratives": "[...]"}
    # instead of {"narratives": [...]} -- a JSON string, not a native array.
    payload = json.dumps([{"account": "Acme Co", "status": "doing fine", "recommended_action": "Follow up"}])
    _patch_anthropic(monkeypatch, _fake_tool_response(anthropic_client._TOOL_NAME, {"narratives": payload}))

    result = anthropic_client._synthesize_batch([{"account": "Acme Co", "day": 1, "stage": "live", "context": []}])

    assert result == {"Acme Co": {"status": "doing fine", "recommended_action": "Follow up"}}


def test_synthesize_report_batch_handles_reports_double_encoded_as_a_json_string(monkeypatch):
    payload = json.dumps([{"account": "Acme Co", "status": "doing fine", "recent_work": "shipped a fix"}])
    _patch_anthropic(monkeypatch, _fake_tool_response(anthropic_client._REPORT_TOOL_NAME, {"reports": payload}))

    result = anthropic_client._synthesize_report_batch(
        [{"account": "Acme Co", "day": 1, "stage": "live", "is_live": True, "context": []}]
    )

    assert result == {"Acme Co": {"status": "doing fine", "recent_work": "shipped a fix"}}


def test_synthesize_report_batch_handles_a_self_nested_stringified_object(monkeypatch):
    inner = {"reports": [{"account": "Acme Co", "status": "doing fine", "recent_work": "shipped a fix"}]}
    _patch_anthropic(monkeypatch, _fake_tool_response(anthropic_client._REPORT_TOOL_NAME, {"reports": json.dumps(inner)}))

    result = anthropic_client._synthesize_report_batch(
        [{"account": "Acme Co", "day": 1, "stage": "live", "is_live": True, "context": []}]
    )

    assert result == {"Acme Co": {"status": "doing fine", "recent_work": "shipped a fix"}}


def test_synthesize_report_batch_still_works_with_a_normal_native_array(monkeypatch):
    reports = [{"account": "Acme Co", "status": "doing fine", "recent_work": "shipped a fix"}]
    _patch_anthropic(monkeypatch, _fake_tool_response(anthropic_client._REPORT_TOOL_NAME, {"reports": reports}))

    result = anthropic_client._synthesize_report_batch(
        [{"account": "Acme Co", "day": 1, "stage": "live", "is_live": True, "context": []}]
    )

    assert result == {"Acme Co": {"status": "doing fine", "recent_work": "shipped a fix"}}
