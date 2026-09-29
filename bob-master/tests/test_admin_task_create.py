"""
Tests app.tasks.admin_task_create.create_admin_task against fake Atlas/ClickUp
clients -- proves Delivery-list targeting, email->assignee resolution (and
that an unresolvable email doesn't block creation), due/start date epoch-ms
conversion, the immediate reflect-into-Atlas push, and the three real setup
errors (unknown account, no ClickUp folder, no Delivery list) surfacing as
ValueError for the router to turn into a 400.
"""
import pytest

import app.tasks.admin_task_create as mod


class _FakeAtlasClient:
    accounts: list = []
    pushed: list = []  # [(atlas_id, tasks), ...]

    def get_all_accounts(self):
        return _FakeAtlasClient.accounts

    def post_admin_tasks(self, atlas_id, tasks):
        _FakeAtlasClient.pushed.append((atlas_id, tasks))


class _FakeClickUpClient:
    folder_lists: dict = {}  # folder_id -> [ {id, name}, ... ]
    members_by_email: dict = {}  # email -> {id, username, email}
    created: list = []  # kwargs passed to create_task

    def get_folder_lists(self, folder_id):
        return _FakeClickUpClient.folder_lists.get(folder_id, [])

    def find_member_by_email(self, email):
        return _FakeClickUpClient.members_by_email.get(email)

    def create_task(self, list_id, name, *, description="", tags=None, assignees=None, due_date_ms=None, start_date_ms=None):
        _FakeClickUpClient.created.append({
            "list_id": list_id, "name": name, "tags": tags,
            "assignees": assignees, "due_date_ms": due_date_ms, "start_date_ms": start_date_ms,
        })
        return {"id": "new-task-1", "name": name, "url": "https://app.clickup.com/t/new-task-1"}


def _reset():
    _FakeAtlasClient.accounts = []
    _FakeAtlasClient.pushed = []
    _FakeClickUpClient.folder_lists = {}
    _FakeClickUpClient.members_by_email = {}
    _FakeClickUpClient.created = []


def _setup(monkeypatch):
    _reset()
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUpClient)


def _account(atlas_id, folder_id):
    return {"id": atlas_id, "integrations": {"clickupFolderId": folder_id}}


def test_creates_task_in_the_delivery_prefixed_list(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.folder_lists = {
        "folder-1": [{"id": "list-website", "name": "Website"}, {"id": "list-delivery", "name": "Delivery - Ongoing"}],
    }

    result = mod.create_admin_task("atlas-1", "Confirm domain access")

    assert _FakeClickUpClient.created[0]["list_id"] == "list-delivery"
    assert _FakeClickUpClient.created[0]["name"] == "Confirm domain access"
    assert _FakeClickUpClient.created[0]["tags"] == ["action"]
    assert result["clickupTaskId"] == "new-task-1"
    assert result["url"] == "https://app.clickup.com/t/new-task-1"


def test_delivery_list_match_is_case_insensitive_prefix(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.folder_lists = {"folder-1": [{"id": "list-1", "name": "delivery"}]}

    mod.create_admin_task("atlas-1", "Task")

    assert _FakeClickUpClient.created[0]["list_id"] == "list-1"


def test_resolves_assignee_email_to_clickup_user_id(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.folder_lists = {"folder-1": [{"id": "list-1", "name": "Delivery"}]}
    _FakeClickUpClient.members_by_email = {"jane@advancedmarketers.co": {"id": 999, "username": "Jane Doe", "email": "jane@advancedmarketers.co"}}

    result = mod.create_admin_task("atlas-1", "Task", assignee_email="jane@advancedmarketers.co")

    assert _FakeClickUpClient.created[0]["assignees"] == [999]
    assert result["assignee"] == "Jane Doe"


def test_unresolvable_assignee_email_does_not_block_creation(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.folder_lists = {"folder-1": [{"id": "list-1", "name": "Delivery"}]}

    result = mod.create_admin_task("atlas-1", "Task", assignee_email="typo@nowhere.co")

    assert _FakeClickUpClient.created[0]["assignees"] is None
    assert result["assignee"] == ""


def test_due_and_start_date_are_converted_to_epoch_ms(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.folder_lists = {"folder-1": [{"id": "list-1", "name": "Delivery"}]}

    mod.create_admin_task("atlas-1", "Task", due_date="2026-09-30T00:00:00+00:00", start_date="2026-09-20T00:00:00+00:00")

    assert _FakeClickUpClient.created[0]["due_date_ms"] == 1790726400000
    assert _FakeClickUpClient.created[0]["start_date_ms"] == 1789862400000


def test_reflects_the_new_task_into_atlas_immediately(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.folder_lists = {"folder-1": [{"id": "list-1", "name": "Delivery"}]}

    mod.create_admin_task("atlas-1", "Task")

    assert len(_FakeAtlasClient.pushed) == 1
    atlas_id, tasks = _FakeAtlasClient.pushed[0]
    assert atlas_id == "atlas-1"
    assert tasks[0]["clickupTaskId"] == "new-task-1"
    assert tasks[0]["status"] == "open"
    assert tasks[0]["sourceMeeting"] == "Daily Leads Standup"


def test_unknown_atlas_id_raises_value_error(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = []

    with pytest.raises(ValueError, match="no Atlas account found"):
        mod.create_admin_task("does-not-exist", "Task")


def test_account_with_no_clickup_folder_raises_value_error(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [{"id": "atlas-1", "integrations": {}}]

    with pytest.raises(ValueError, match="no ClickUp folder"):
        mod.create_admin_task("atlas-1", "Task")


def test_folder_with_no_delivery_list_raises_value_error(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.folder_lists = {"folder-1": [{"id": "list-1", "name": "Website"}]}

    with pytest.raises(ValueError, match="Delivery"):
        mod.create_admin_task("atlas-1", "Task")
