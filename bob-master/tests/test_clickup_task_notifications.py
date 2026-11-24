"""ClickUp task created/completed -> internal Slack channel (2026-11-23)."""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import app.routers.clickup_webhook as router_mod
import app.tasks.clickup_task_notifications as mod
from app.config import get_settings

SPACE = "90147058709"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("GHL_API_KEY", "GHL_LOCATION_ID", "CLICKUP_API_TOKEN", "SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY", "ATLAS_API_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ9")
    monkeypatch.setenv("CLICKUP_WEBHOOK_SECRET", "s3cret")
    get_settings.cache_clear()
    mod.reset_cache()
    yield
    get_settings.cache_clear()


def _task(**over):
    t = {
        "id": "t1", "name": "Build <landing> page", "url": "https://app.clickup.com/t/t1",
        "space": {"id": SPACE}, "folder": {"id": "F1", "hidden": False}, "list": {"id": "L1", "name": "Delivery"},
        "assignees": [{"username": "Ana"}], "creator": {"username": "Chris"},
    }
    t.update(over)
    return t


class FakeClickUp:
    def __init__(self, task):
        self.task = task

    def get_task(self, task_id):
        return self.task


class FakeAtlas:
    def __init__(self, accounts):
        self.accounts = accounts

    def get_all_accounts(self):
        return self.accounts


class FakeSlack:
    def __init__(self, names=None, channels=None):
        self.names = names or {}
        self.channels = channels or []
        self.posts = []

    def get_channel_name(self, cid):
        return self.names.get(cid)

    def list_channels(self):
        return self.channels

    def post_message(self, channel, text):
        self.posts.append((channel, text))


def _created(history_id="h1"):
    return {"event": "taskCreated", "task_id": "t1", "history_items": [{"id": history_id, "user": {"username": "Chris"}}]}


def _status(before, after, history_id="h2"):
    return {"event": "taskStatusUpdated", "task_id": "t1", "history_items": [{
        "id": history_id, "field": "status", "user": {"username": "Ana"},
        "before": {"status": "x", "type": before}, "after": {"status": "y", "type": after},
    }]}


def _run(payload, task, accounts, slack):
    return mod.process_event(
        payload, clickup_factory=lambda: FakeClickUp(task), atlas_factory=lambda: FakeAtlas(accounts), slack_factory=lambda: slack,
    )


def test_classify():
    assert mod.classify_event(_created())[0] == "created"
    assert mod.classify_event(_status("open", "closed"))[0] == "completed"
    assert mod.classify_event(_status("custom", "done"))[0] == "completed"
    assert mod.classify_event(_status("open", "custom")) is None
    assert mod.classify_event(_status("done", "closed")) is None  # already done
    assert mod.classify_event({"event": "taskUpdated"}) is None


def test_internal_name_derivation():
    assert mod.internal_channel_name_from_shared("advancedmarketers-x-c3-sewer-and-drains") == "internal-c3-sewer-and-drains"
    assert mod.internal_channel_name_from_shared("advancedmarketers_x_uv_roofing") == "internal-uv-roofing"
    assert mod.internal_channel_name_from_shared("cambria-windows-x-am") is None
    assert mod.internal_channel_name_from_shared(None) is None


def test_match_prefers_list_then_parent():
    parent = {"id": "p", "integrations": {"clickupFolderId": "F1"}}
    child = {"id": "c", "parentId": "p", "integrations": {"clickupFolderId": "F1", "clickupListId": "L9"}}
    assert mod.match_account([child, parent], "F1", "L1")["id"] == "p"
    assert mod.match_account([child, parent], "F1", "L9")["id"] == "c"
    assert mod.match_account([parent], "F2", "L2") is None


def test_posts_created_to_explicit_internal_field():
    slack = FakeSlack()
    acct = {"companyName": "Acme", "integrations": {"clickupFolderId": "F1", "internalSlackChannelId": "CINT", "slackChannelId": "CSHARED"}}
    out = _run(_created(), _task(), [acct], slack)
    assert out["outcome"] == "posted"
    channel, text = slack.posts[0]
    assert channel == "CINT"
    assert "Task created" in text and "&lt;landing&gt;" in text and "assigned Ana" in text and "by Chris" in text


def test_derives_internal_channel_and_never_posts_shared():
    slack = FakeSlack(names={"CSHARED": "advancedmarketers-x-acme"}, channels=[{"name": "internal-acme", "id": "CINT2", "created": 1}])
    acct = {"companyName": "Acme", "integrations": {"clickupFolderId": "F1", "slackChannelId": "CSHARED"}}
    assert _run(_status("open", "closed"), _task(), [acct], slack)["outcome"] == "posted"
    assert slack.posts[0][0] == "CINT2" and "Task completed" in slack.posts[0][1]


def test_no_internal_channel_drops_rather_than_posting_shared():
    slack = FakeSlack(names={"CSHARED": "advancedmarketers-x-acme"}, channels=[])
    acct = {"companyName": "Acme", "integrations": {"clickupFolderId": "F1", "slackChannelId": "CSHARED"}}
    assert _run(_created(), _task(), [acct], slack)["outcome"] == "no_internal_channel"
    assert slack.posts == []


def test_unmapped_folder_and_other_space_and_duplicates():
    slack = FakeSlack()
    acct = {"integrations": {"clickupFolderId": "F1", "internalSlackChannelId": "CINT"}}
    assert _run(_created("a"), _task(folder={"id": "ZZ"}), [acct], slack)["outcome"] == "no_account"
    assert _run(_created("b"), _task(space={"id": "999"}), [acct], slack)["outcome"] == "other_space"
    assert _run(_created("c"), _task(), [acct], slack)["outcome"] == "posted"
    assert _run(_created("c"), _task(), [acct], slack)["outcome"] == "duplicate"
    assert len(slack.posts) == 1


def _sign(body: bytes, secret="s3cret"):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_route_signature(monkeypatch):
    from app.main import app

    seen = []
    monkeypatch.setattr(router_mod, "process_event", lambda p: seen.append(p) or {"outcome": "posted"})
    client = TestClient(app)
    body = json.dumps(_created()).encode()

    assert client.post("/webhooks/clickup", content=body, headers={"X-Signature": "nope"}).status_code == 401
    r = client.post("/webhooks/clickup", content=body, headers={"X-Signature": _sign(body)})
    assert r.status_code == 200 and seen and seen[0]["task_id"] == "t1"


def test_webhook_route_503_without_secret(monkeypatch):
    from app.main import app

    monkeypatch.delenv("CLICKUP_WEBHOOK_SECRET")
    get_settings.cache_clear()
    body = b"{}"
    assert TestClient(app).post("/webhooks/clickup", content=body, headers={"X-Signature": _sign(body)}).status_code == 503


def test_fallback_matches_old_underscore_names_by_company_and_prefers_newest():
    slack = FakeSlack(channels=[
        {"name": "internal-sky_clean_air", "id": "OLD", "created": 1},
        {"name": "internal-sky-clean-air", "id": "NEW", "created": 2},
        {"name": "internal-sky-clean-air-pros", "id": "OTHER", "created": 3},
    ])
    acct = {"companyName": "Sky Clean Air", "integrations": {"clickupFolderId": "F1"}}
    assert mod.resolve_internal_channel(acct, slack) == "NEW"
    assert mod.resolve_internal_channel({"companyName": "Sky", "integrations": {}}, slack) is None
