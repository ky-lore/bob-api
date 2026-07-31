"""
Tests SlackClient.join_all_public_channels() directly against a fake inner
WebClient -- the real bug this guards against (Bob, 2026-07-31): a live run
against ~750 channels tried to join every archived one too, which Slack
rejects unconditionally (is_archived), producing hundreds of noise "failures"
for something that could never have succeeded.
"""
from app.config import get_settings
from app.integrations.slack import SlackClient


class _FakeWebClient:
    def __init__(self, channels, join_error=None):
        self._channels = channels
        self._join_error = join_error
        self.join_attempts: list[str] = []

    def conversations_list(self, types, cursor, limit):
        return {"channels": self._channels}

    def conversations_join(self, channel):
        self.join_attempts.append(channel)
        if self._join_error:
            raise self._join_error
        return {"ok": True}


def _client(monkeypatch) -> SlackClient:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("GHL_API_KEY", "x")
    monkeypatch.setenv("GHL_LOCATION_ID", "x")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    get_settings.cache_clear()
    return SlackClient()


def test_join_all_public_channels_skips_archived_without_attempting_to_join(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeWebClient(
        channels=[
            {"id": "C1", "name": "active-not-member", "is_member": False, "is_archived": False},
            {"id": "C2", "name": "archived-not-member", "is_member": False, "is_archived": True},
            {"id": "C3", "name": "already-in", "is_member": True, "is_archived": False},
        ]
    )
    client._client = fake

    result = client.join_all_public_channels()

    assert result["joined"] == ["active-not-member"]
    assert result["already_in"] == ["already-in"]
    assert result["skipped_archived"] == ["archived-not-member"]
    assert result["failed"] == []
    assert fake.join_attempts == ["C1"]  # the archived channel never got a join call at all


def test_join_all_public_channels_records_genuine_failures(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeWebClient(
        channels=[{"id": "C1", "name": "no-permission", "is_member": False, "is_archived": False}],
        join_error=RuntimeError("missing_scope"),
    )
    client._client = fake

    result = client.join_all_public_channels()

    assert result["joined"] == []
    assert result["skipped_archived"] == []
    assert len(result["failed"]) == 1
    assert "no-permission" in result["failed"][0]
    assert "missing_scope" in result["failed"][0]


def test_join_all_public_channels_idempotent_when_all_already_members(monkeypatch):
    client = _client(monkeypatch)
    fake = _FakeWebClient(channels=[{"id": "C1", "name": "already-in", "is_member": True, "is_archived": False}])
    client._client = fake

    result = client.join_all_public_channels()

    assert result == {"joined": [], "already_in": ["already-in"], "skipped_archived": [], "failed": []}
    assert fake.join_attempts == []
