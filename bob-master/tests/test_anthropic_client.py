"""
Tests app.integrations.anthropic_client's batching logic directly (mocking
_synthesize_batch, not the real API) — the behavior that matters here is how
multiple batches combine and degrade, not the Claude call itself.
"""
from app.integrations import anthropic_client


def _accounts(n: int) -> list[dict]:
    return [{"account": f"Account {i}", "day": i, "stage": "onboarding", "context": [f"flag {i}"]} for i in range(n)]


def test_synthesize_blocking_narratives_splits_into_batches(monkeypatch):
    calls = []

    def _fake_batch(batch):
        calls.append(len(batch))
        return {a["account"]: f"narrative for {a['account']}" for a in batch}

    monkeypatch.setattr(anthropic_client, "_synthesize_batch", _fake_batch)
    monkeypatch.setattr(anthropic_client, "_BATCH_SIZE", 20)

    result = anthropic_client.synthesize_blocking_narratives(_accounts(45))

    assert calls == [20, 20, 5]
    assert len(result) == 45
    assert result["Account 0"] == "narrative for Account 0"


def test_synthesize_blocking_narratives_partial_batch_failure_keeps_successful_ones(monkeypatch):
    def _fake_batch(batch):
        if batch[0]["account"] == "Account 20":
            raise RuntimeError("stop_reason=max_tokens")
        return {a["account"]: "ok" for a in batch}

    monkeypatch.setattr(anthropic_client, "_synthesize_batch", _fake_batch)
    monkeypatch.setattr(anthropic_client, "_BATCH_SIZE", 20)

    result = anthropic_client.synthesize_blocking_narratives(_accounts(45))

    # First batch (0-19) and third batch (40-44) succeeded; second batch (20-39) failed.
    assert "Account 0" in result
    assert "Account 44" in result
    assert "Account 20" not in result
    assert len(result) == 25


def test_synthesize_blocking_narratives_raises_only_if_every_batch_fails(monkeypatch):
    def _always_fail(batch):
        raise RuntimeError("boom")

    monkeypatch.setattr(anthropic_client, "_synthesize_batch", _always_fail)
    monkeypatch.setattr(anthropic_client, "_BATCH_SIZE", 20)

    try:
        anthropic_client.synthesize_blocking_narratives(_accounts(25))
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "boom" in str(exc)


def test_synthesize_blocking_narratives_empty_input_returns_empty_without_calling_batch(monkeypatch):
    def _should_not_be_called(batch):
        raise AssertionError("should not be called for empty input")

    monkeypatch.setattr(anthropic_client, "_synthesize_batch", _should_not_be_called)
    assert anthropic_client.synthesize_blocking_narratives([]) == {}
