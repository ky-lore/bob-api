"""
Tests app.tasks.standup_action_items.sync_standup_action_items against fake
ClickUp + Atlas clients -- proves folder-id matching (the join key between a
ClickUp task and the Atlas account that owns its folder), the closed/open
status mapping, epoch-ms -> ISO due-date conversion, and per-account soft-fail
on push (one account's Atlas push failing must never block the rest).
"""
import app.tasks.standup_action_items as mod


class _FakeAtlasClient:
    accounts: list = []
    fail_for_atlas_id: str | None = None
    pushed: list = []  # [(atlas_id, tasks), ...]

    def get_all_accounts(self):
        return _FakeAtlasClient.accounts

    def post_admin_tasks(self, atlas_id, tasks):
        if atlas_id == _FakeAtlasClient.fail_for_atlas_id:
            raise RuntimeError("atlas is down")
        _FakeAtlasClient.pushed.append((atlas_id, tasks))


class _FakeClickUpClient:
    tasks: list = []

    def get_team_tasks_by_tag(self, tag):
        return _FakeClickUpClient.tasks


def _reset():
    _FakeAtlasClient.accounts = []
    _FakeAtlasClient.fail_for_atlas_id = None
    _FakeAtlasClient.pushed = []
    _FakeClickUpClient.tasks = []


def _setup(monkeypatch):
    _reset()
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUpClient)


def _account(atlas_id, folder_id, *, is_active=True):
    return {"id": atlas_id, "isActive": is_active, "integrations": {"clickupFolderId": folder_id}}


def _clickup_task(task_id, folder_id, *, name="Do the thing", assignees=None, due_date=None, closed=False, url=""):
    return {
        "id": task_id,
        "name": name,
        "folder": {"id": folder_id, "name": "some folder"},
        "assignees": assignees or [],
        "due_date": due_date,
        "status": {"type": "closed" if closed else "open"},
        "url": url,
    }


def test_matches_tasks_by_folder_id_and_pushes_to_atlas(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.tasks = [
        _clickup_task(
            "task-1", "folder-1",
            name="Confirm payment method",
            assignees=[{"username": "Simon Ting"}],
            url="https://app.clickup.com/t/task-1",
        )
    ]

    result = mod.sync_standup_action_items()

    assert result["tasks_found"] == 1
    assert result["accounts_matched"] == 1
    assert result["accounts_unmatched"] == 0
    assert result["pushed"] == 1
    assert result["user_errors"] == []
    assert len(_FakeAtlasClient.pushed) == 1
    atlas_id, tasks = _FakeAtlasClient.pushed[0]
    assert atlas_id == "atlas-1"
    assert tasks == [{
        "clickupTaskId": "task-1",
        "title": "Confirm payment method",
        "assignee": "Simon Ting",
        "dueDate": None,
        "status": "open",
        "sourceMeeting": "Daily Leads Standup",
        "url": "https://app.clickup.com/t/task-1",
    }]


def test_unmatched_folder_is_soft_skipped_not_pushed_or_errored(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.tasks = [_clickup_task("task-1", "folder-does-not-exist-anywhere")]

    result = mod.sync_standup_action_items()

    assert result["accounts_unmatched"] == 1
    assert result["pushed"] == 0
    assert result["user_errors"] == []
    assert _FakeAtlasClient.pushed == []


def test_inactive_account_is_excluded_from_the_folder_map(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1", is_active=False)]
    _FakeClickUpClient.tasks = [_clickup_task("task-1", "folder-1")]

    result = mod.sync_standup_action_items()

    assert result["accounts_unmatched"] == 1
    assert result["pushed"] == 0


def test_a_failed_push_is_recorded_as_a_soft_error_and_does_not_block_others(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1"), _account("atlas-2", "folder-2")]
    _FakeAtlasClient.fail_for_atlas_id = "atlas-1"
    _FakeClickUpClient.tasks = [
        _clickup_task("task-1", "folder-1"),
        _clickup_task("task-2", "folder-2"),
    ]

    result = mod.sync_standup_action_items()

    assert result["accounts_matched"] == 2
    assert result["pushed"] == 1  # only atlas-2's task actually landed
    assert result["user_errors"] == [{"atlas_id": "atlas-1", "error": "atlas is down"}]
    assert [atlas_id for atlas_id, _ in _FakeAtlasClient.pushed] == ["atlas-2"]


def test_closed_clickup_status_maps_to_done(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.tasks = [_clickup_task("task-1", "folder-1", closed=True)]

    mod.sync_standup_action_items()

    _, tasks = _FakeAtlasClient.pushed[0]
    assert tasks[0]["status"] == "done"


def test_due_date_is_converted_from_epoch_ms_to_iso(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    # 2026-09-29T00:00:00Z
    _FakeClickUpClient.tasks = [_clickup_task("task-1", "folder-1", due_date="1790640000000")]

    mod.sync_standup_action_items()

    _, tasks = _FakeAtlasClient.pushed[0]
    assert tasks[0]["dueDate"] == "2026-09-29T00:00:00+00:00"


def test_task_with_no_assignees_gets_an_empty_string_assignee(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.tasks = [_clickup_task("task-1", "folder-1", assignees=[])]

    mod.sync_standup_action_items()

    _, tasks = _FakeAtlasClient.pushed[0]
    assert tasks[0]["assignee"] == ""


def test_custom_tag_is_passed_through_to_the_clickup_client(monkeypatch):
    _setup(monkeypatch)
    calls = []
    _FakeClickUpClient.get_team_tasks_by_tag = lambda self, tag: calls.append(tag) or []

    mod.sync_standup_action_items(tag="custom-tag")

    assert calls == ["custom-tag"]
