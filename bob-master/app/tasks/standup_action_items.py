"""
Syncs standup action items from ClickUp into Atlas (2026-09-28).

Team leads walk every account on the daily internal "Daily Leads Standup" and
are now required to log each action item as a live ClickUp task before moving
to the next account (real problem this fixes: verbal commitments with no
deadline/owner get silently dropped and re-raised the next day -- see chat
history, 2026-09-28). Bob is a pure relay here: it never persists these tasks
itself (no DB session needed) -- it polls every ClickUp task workspace-wide,
keeps the ones matching _NAME_FILTER, matches each task's folder back to the
Atlas account that owns that ClickUp folder, and pushes the matched tasks
into Atlas's own AdminTask collection via POST /api/accounts/:id/admin-tasks
(upserted by clickupTaskId there, so re-running this is always safe).

Originally matched on a ClickUp tag (`action`); switched (2026-09-29, Chris)
to a plain substring match against the task NAME instead -- one less thing
for a team lead to remember mid-meeting than applying a tag. See
get_all_team_tasks's docstring for why this means pulling every workspace
task rather than filtering server-side.

Manual-trigger only for now, not wired into the scheduler -- same rollout
posture as zoom_call_sync.py and atlas_campaign_push.py: review a real run's
output before anything runs unattended.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.integrations.atlas_client import AtlasClient
from app.integrations.clickup import ClickUpClient

_NAME_FILTER = "admin"
_SOURCE_MEETING = "Daily Leads Standup"


def _atlas_id_by_folder() -> dict[str, str]:
    """clickupFolderId -> atlas_id, for every active Atlas account that has
    one on file. This is the join key: a standup action item's ClickUp task
    lands in *some* client's folder, and every Atlas account already carries
    its own folder id (integrations.clickupFolderId) -- no extra lookup or
    fuzzy name matching needed, unlike Zoom's topic-based correlation."""
    accounts = [a for a in AtlasClient().get_all_accounts() if a.get("isActive")]
    return {
        (a.get("integrations") or {}).get("clickupFolderId"): a["id"]
        for a in accounts
        if (a.get("integrations") or {}).get("clickupFolderId")
    }


def _task_payload(task: dict[str, Any]) -> dict[str, Any]:
    assignees = task.get("assignees") or []
    assignee_name = assignees[0].get("username", "") if assignees else ""
    due_date_ms = task.get("due_date")
    start_date_ms = task.get("start_date")
    return {
        "clickupTaskId": task["id"],
        "title": task.get("name") or "(untitled)",
        "assignee": assignee_name,
        # ClickUp due_date/start_date are epoch-ms strings (or None); Atlas's
        # admin-tasks route does `new Date(...)`, which parses an ISO string,
        # not raw ms -- convert here rather than push a value the other side
        # can't read.
        "dueDate": _ms_to_iso(due_date_ms) if due_date_ms else None,
        "startDate": _ms_to_iso(start_date_ms) if start_date_ms else None,
        "status": "done" if task.get("status", {}).get("type") == "closed" else "open",
        "sourceMeeting": _SOURCE_MEETING,
        "url": task.get("url", ""),
    }


def _ms_to_iso(ms: str) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()


def sync_standup_action_items(name_filter: str = _NAME_FILTER) -> dict[str, Any]:
    """Returns {"tasks_found", "accounts_matched", "accounts_unmatched",
    "pushed", "user_errors": [{"atlas_id", "error"}, ...]}. Soft-fails per
    account -- one account's push failing (Atlas down, bad id, etc.) never
    blocks the rest from syncing.

    name_filter match is case-insensitive substring, not exact/whole-word --
    same lenient posture as this codebase's other best-effort text matching
    (see account_name_matching.py): "admin" as a substring is a much lower
    bar to catch a real intent than requiring it be its own word, and a
    false positive here just means one extra task considered, not a wrong
    account correlation."""
    folder_to_atlas_id = _atlas_id_by_folder()
    name_filter_lower = name_filter.lower()
    tasks = [
        t for t in ClickUpClient().get_all_team_tasks()
        if name_filter_lower in (t.get("name") or "").lower()
    ]

    by_atlas_id: dict[str, list[dict[str, Any]]] = {}
    unmatched = 0
    for task in tasks:
        folder_id = (task.get("folder") or {}).get("id")
        atlas_id = folder_to_atlas_id.get(folder_id)
        if not atlas_id:
            unmatched += 1
            continue
        by_atlas_id.setdefault(atlas_id, []).append(_task_payload(task))

    atlas = AtlasClient()
    pushed = 0
    user_errors: list[dict[str, str]] = []
    for atlas_id, account_tasks in by_atlas_id.items():
        try:
            atlas.post_admin_tasks(atlas_id, account_tasks)
            pushed += len(account_tasks)
        except Exception as exc:
            user_errors.append({"atlas_id": atlas_id, "error": str(exc)})

    return {
        "tasks_found": len(tasks),
        "accounts_matched": len(by_atlas_id),
        "accounts_unmatched": unmatched,
        "pushed": pushed,
        "user_errors": user_errors,
    }
