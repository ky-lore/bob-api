"""
Tests app.tasks.standup_action_items.sync_standup_action_items against fake
ClickUp + Atlas clients -- proves folder-id matching (the join key between a
ClickUp task and the Atlas account that owns its folder), the name-substring
filter (2026-09-29, replacing an earlier ClickUp-tag filter -- see the
module's docstring), the closed/open status mapping, epoch-ms -> ISO
due-date conversion, and per-account soft-fail on push (one account's Atlas
push failing must never block the rest).
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
    """get_all_team_tasks backs the default (accounts_filter=None,
    full-workspace) path -- returns every fixture task unconditionally,
    same as the real sweep; the name-substring filtering under test happens
    in sync_standup_action_items itself, not here. get_folder_lists/
    get_list_tasks back the SCOPED path (accounts_filter set), mirroring
    test_atlas_report.py's _FakeClickUp fixture convention -- lists_by_folder
    keyed by folder_id, tasks_by_list keyed by (list_id, page)."""
    tasks: list = []
    lists_by_folder: dict = {}
    tasks_by_list: dict = {}
    get_all_team_tasks_calls: int = 0
    get_folder_lists_calls: list = []

    def get_all_team_tasks(self):
        _FakeClickUpClient.get_all_team_tasks_calls += 1
        return _FakeClickUpClient.tasks

    def get_folder_lists(self, folder_id):
        _FakeClickUpClient.get_folder_lists_calls.append(folder_id)
        return _FakeClickUpClient.lists_by_folder.get(folder_id, [])

    def get_list_tasks(self, list_id, include_closed=True, page=0):
        return {"tasks": _FakeClickUpClient.tasks_by_list.get((list_id, page), [])}


def _reset():
    _FakeAtlasClient.accounts = []
    _FakeAtlasClient.fail_for_atlas_id = None
    _FakeAtlasClient.pushed = []
    _FakeClickUpClient.tasks = []
    _FakeClickUpClient.lists_by_folder = {}
    _FakeClickUpClient.tasks_by_list = {}
    _FakeClickUpClient.get_all_team_tasks_calls = 0
    _FakeClickUpClient.get_folder_lists_calls = []


def _setup(monkeypatch):
    _reset()
    monkeypatch.setattr(mod, "AtlasClient", _FakeAtlasClient)
    monkeypatch.setattr(mod, "ClickUpClient", _FakeClickUpClient)


def _account(atlas_id, folder_id, *, is_active=True):
    return {"id": atlas_id, "isActive": is_active, "integrations": {"clickupFolderId": folder_id}}


def _clickup_task(task_id, folder_id, *, name="Admin: Do the thing", assignees=None, due_date=None, start_date=None, closed=False, url=""):
    return {
        "id": task_id,
        "name": name,
        "folder": {"id": folder_id, "name": "some folder"},
        "assignees": assignees or [],
        "due_date": due_date,
        "start_date": start_date,
        "status": {"type": "closed" if closed else "open"},
        "url": url,
    }


def test_matches_tasks_by_folder_id_and_pushes_to_atlas(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.tasks = [
        _clickup_task(
            "task-1", "folder-1",
            name="Admin: Confirm payment method",
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
        "title": "Admin: Confirm payment method",
        "assignee": "Simon Ting",
        "dueDate": None,
        "startDate": None,
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


def test_start_date_is_converted_from_epoch_ms_to_iso(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    # 2026-09-14T00:00:00Z
    _FakeClickUpClient.tasks = [_clickup_task("task-1", "folder-1", start_date="1789344000000")]

    mod.sync_standup_action_items()

    _, tasks = _FakeAtlasClient.pushed[0]
    assert tasks[0]["startDate"] == "2026-09-14T00:00:00+00:00"


def test_task_with_no_assignees_gets_an_empty_string_assignee(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.tasks = [_clickup_task("task-1", "folder-1", assignees=[])]

    mod.sync_standup_action_items()

    _, tasks = _FakeAtlasClient.pushed[0]
    assert tasks[0]["assignee"] == ""


def test_tasks_without_the_name_filter_are_excluded(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.tasks = [
        _clickup_task("task-1", "folder-1", name="Admin: follow up on billing"),
        _clickup_task("task-2", "folder-1", name="Unrelated ClickUp task, not a standup item"),
    ]

    result = mod.sync_standup_action_items()

    assert result["tasks_found"] == 1
    _, tasks = _FakeAtlasClient.pushed[0]
    assert [t["clickupTaskId"] for t in tasks] == ["task-1"]


def test_name_filter_match_is_case_insensitive(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.tasks = [_clickup_task("task-1", "folder-1", name="ADMIN: follow up on billing")]

    result = mod.sync_standup_action_items()

    assert result["tasks_found"] == 1


def test_custom_name_filter_is_honored(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.tasks = [
        _clickup_task("task-1", "folder-1", name="Admin: follow up on billing"),
        _clickup_task("task-2", "folder-1", name="Custom-tag: follow up on billing"),
    ]

    result = mod.sync_standup_action_items(name_filter="custom-tag")

    assert result["tasks_found"] == 1
    _, tasks = _FakeAtlasClient.pushed[0]
    assert [t["clickupTaskId"] for t in tasks] == ["task-2"]


def test_no_accounts_filter_uses_the_full_workspace_sweep(monkeypatch):
    # The default (standalone manual-trigger endpoint) behavior must stay
    # exactly what it was -- confirms get_all_team_tasks is used and the
    # scoped folder-walk path is never touched.
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.tasks = [_clickup_task("task-1", "folder-1")]

    mod.sync_standup_action_items()

    assert _FakeClickUpClient.get_all_team_tasks_calls == 1
    assert _FakeClickUpClient.get_folder_lists_calls == []


def test_accounts_filter_scopes_to_only_matching_accounts_folders(monkeypatch):
    # 2026-09-30: confirmed the unscoped full-workspace sweep was ~9 of a
    # CMDCTR run's ~10 minutes. With accounts_filter set, only the matching
    # accounts' own folders get walked (get_folder_lists/get_list_tasks) --
    # get_all_team_tasks must not be called at all, and a task that would
    # have matched but lives in a filtered-OUT account's folder must never
    # be found (that folder is never even requested).
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [
        _account("scoped-in", "folder-in"),
        _account("scoped-out", "folder-out"),
    ]
    _FakeClickUpClient.lists_by_folder = {
        "folder-in": [{"id": "list-in"}],
        "folder-out": [{"id": "list-out"}],
    }
    _FakeClickUpClient.tasks_by_list = {
        ("list-in", 0): [_clickup_task("task-in", "folder-in", name="Admin: in scope")],
        ("list-out", 0): [_clickup_task("task-out", "folder-out", name="Admin: out of scope")],
    }

    result = mod.sync_standup_action_items(accounts_filter=lambda a: a["id"] == "scoped-in")

    assert _FakeClickUpClient.get_all_team_tasks_calls == 0
    assert _FakeClickUpClient.get_folder_lists_calls == ["folder-in"]
    assert result["tasks_found"] == 1
    assert len(_FakeAtlasClient.pushed) == 1
    atlas_id, tasks = _FakeAtlasClient.pushed[0]
    assert atlas_id == "scoped-in"
    assert [t["clickupTaskId"] for t in tasks] == ["task-in"]


def test_scoped_fetch_paginates_list_tasks(monkeypatch):
    _setup(monkeypatch)
    _FakeAtlasClient.accounts = [_account("atlas-1", "folder-1")]
    _FakeClickUpClient.lists_by_folder = {"folder-1": [{"id": "list-1"}]}
    _FakeClickUpClient.tasks_by_list = {
        ("list-1", 0): {
            "tasks": [_clickup_task("task-1", "folder-1", name="Admin: page one")],
            "last_page": False,
        },
        ("list-1", 1): {
            "tasks": [_clickup_task("task-2", "folder-1", name="Admin: page two")],
            "last_page": True,
        },
    }
    # The default get_list_tasks fake wraps its return in {"tasks": ...}
    # unconditionally -- override here since this test's fixture values
    # already carry both "tasks" and "last_page" per page.
    def _paginated_get_list_tasks(self, list_id, include_closed=True, page=0):
        return _FakeClickUpClient.tasks_by_list[(list_id, page)]

    monkeypatch.setattr(_FakeClickUpClient, "get_list_tasks", _paginated_get_list_tasks)

    result = mod.sync_standup_action_items(accounts_filter=lambda a: True)

    assert result["tasks_found"] == 2
    _, tasks = _FakeAtlasClient.pushed[0]
    assert {t["clickupTaskId"] for t in tasks} == {"task-1", "task-2"}
