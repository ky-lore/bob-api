"""
Creates a new standup action item directly in ClickUp (2026-09-28) -- the
reverse direction of app/tasks/standup_action_items.py's sync, which only
ever reads FROM ClickUp. Triggered one task at a time from Atlas's Command
Center "create action task" UI.

Lands in whatever list inside the account's ClickUp folder is named
"Delivery..." -- a real, existing per-client convention (confirmed with
Chris, 2026-09-28: "should always be a list prefixed Delivery"), not
something invented here. Title gets an "Admin: " prefix (2026-09-29,
replacing an "action" ClickUp tag -- see standup_action_items.py's module
docstring for why) so it matches _NAME_FILTER and keeps flowing through the
regular sync/relay the same as any other action item from then on -- this
isn't a parallel system, and without the prefix a future re-sync could never
find this task again to pick up a status change (e.g. marking it done once
it's closed in ClickUp).

Also reflects the created task straight into Atlas's AdminTask collection
via the existing post_admin_tasks push, rather than waiting for the next
workspace-wide sync run to pick it up -- the whole point is showing up in
Command Center immediately.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.integrations.atlas_client import AtlasClient
from app.integrations.clickup import ClickUpClient
from app.tasks.standup_action_items import _NAME_FILTER

_SOURCE_MEETING = "Daily Leads Standup"


def _with_name_filter(title: str) -> str:
    """Prefixes `title` with _NAME_FILTER unless it's already present
    somewhere in it (case-insensitive) -- avoids a double "Admin: Admin: ..."
    if someone types their own "admin" into the Command Center title field."""
    if _NAME_FILTER.lower() in title.lower():
        return title
    return f"{_NAME_FILTER.capitalize()}: {title}"


def _iso_to_ms(iso: str) -> int:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _find_delivery_list_id(clickup: ClickUpClient, folder_id: str) -> str | None:
    for lst in clickup.get_folder_lists(folder_id):
        if (lst.get("name") or "").strip().lower().startswith("delivery"):
            return lst["id"]
    return None


def create_admin_task(
    atlas_id: str,
    title: str,
    *,
    assignee_email: str | None = None,
    due_date: str | None = None,
    start_date: str | None = None,
) -> dict[str, Any]:
    """Raises ValueError for real setup problems the caller (the create-task
    UI) should surface directly, not soft-fail through: unknown atlas_id, no
    ClickUp folder on file, no Delivery-prefixed list in that folder. An
    unresolvable assignee_email is NOT an error, though -- the task is still
    created unassigned rather than blocking creation over a typo'd email."""
    account = next((a for a in AtlasClient().get_all_accounts() if a.get("id") == atlas_id), None)
    if account is None:
        raise ValueError(f"no Atlas account found for id {atlas_id}")
    folder_id = (account.get("integrations") or {}).get("clickupFolderId")
    if not folder_id:
        raise ValueError("this account has no ClickUp folder on file")

    clickup = ClickUpClient()
    list_id = _find_delivery_list_id(clickup, folder_id)
    if not list_id:
        raise ValueError('no list starting with "Delivery" found in this account\'s ClickUp folder')

    assignee_ids: list[int] = []
    assignee_name = ""
    if assignee_email:
        member = clickup.find_member_by_email(assignee_email)
        if member:
            assignee_ids = [member["id"]]
            assignee_name = member.get("username", "")

    task = clickup.create_task(
        list_id,
        _with_name_filter(title),
        assignees=assignee_ids or None,
        due_date_ms=_iso_to_ms(due_date) if due_date else None,
        start_date_ms=_iso_to_ms(start_date) if start_date else None,
    )

    payload = {
        "clickupTaskId": task["id"],
        "title": task.get("name") or title,
        "assignee": assignee_name,
        "dueDate": due_date,
        "startDate": start_date,
        "status": "open",
        "sourceMeeting": _SOURCE_MEETING,
        "url": task.get("url", ""),
    }
    AtlasClient().post_admin_tasks(atlas_id, [payload])
    return payload
