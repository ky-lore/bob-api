"""
ClickUp "task created" / "task completed" -> the client's INTERNAL Slack
channel (Bob, 2026-11-23).

Flow: ClickUp webhook (space-scoped to AM Delivery 2.0, events taskCreated +
taskStatusUpdated) -> POST /webhooks/clickup (signature-checked, returns
immediately) -> process_event() on a background task:

  1. Decide whether the event is notify-worthy: every taskCreated; a
     taskStatusUpdated only when the status moves INTO a done/closed-type
     status from a not-done one (reopen -> re-close posts again, by design).
  2. GET the task for name/url/folder/list/assignees.
  3. Map folder (and list) -> Atlas account via integrations.clickupFolderId /
     clickupListId. Sub-accounts carry a COPY of their parent's folder id, so
     an exact list match wins, then the parent account, then the first match.
  4. Resolve the account's internal channel: Atlas's internal-channel field
     if present, else derive "internal-<slug>" from the shared channel's name
     ("advancedmarketers-x-<slug>" / "advancedmarketers_x_<slug>") or the
     company name. Internal channels came back 2026-11-18 (internal-<client>
     alongside each new shared channel, bot is a member); as of 2026-11-23
     Atlas's integrations.internalSlackChannelId is set on only 3/173
     accounts and ~20 clients have any internal-* channel at all, so the
     name fallback carries most of the coverage.
  5. Post as the bot (never into the client-shared channel).

Anything that can't be mapped is logged and dropped -- a missing mapping
must never fall through to posting somewhere a client can read.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from app.config import get_settings

logger = logging.getLogger(__name__)

_DONE_TYPES = {"done", "closed"}
# Atlas field candidates for the internal channel, first non-empty wins.
_INTERNAL_FIELDS = ("internalSlackChannelId", "slackInternalChannelId")
_SHARED_PREFIX = re.compile(r"^advanced[-_]?marketers[-_]x[-_]", re.IGNORECASE)

_ACCOUNTS_TTL = 600.0       # Atlas account list
_CHANNELS_TTL = 1800.0      # Slack channel name -> id index
_CHANNELS_MIN_REFRESH = 300.0  # min age before a miss may force a re-list
_DEDUPE_MAX = 2000


class _Cache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fetch_lock = threading.Lock()
        self.accounts: tuple[float, list[dict[str, Any]]] | None = None
        self.channels: tuple[float, dict[str, str]] | None = None
        self.seen: OrderedDict[str, None] = OrderedDict()


_cache = _Cache()


def reset_cache() -> None:
    global _cache
    _cache = _Cache()


# --- pure helpers -----------------------------------------------------------

def classify_event(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None] | None:
    """-> ("created"|"completed", history_item) or None if not notify-worthy."""
    event = payload.get("event")
    items = payload.get("history_items") or []
    if event == "taskCreated":
        return "created", (items[0] if items else None)
    if event == "taskStatusUpdated":
        for item in items:
            if item.get("field") != "status":
                continue
            before = (item.get("before") or {}).get("type")
            after = (item.get("after") or {}).get("type")
            if after in _DONE_TYPES and before not in _DONE_TYPES:
                return "completed", item
    return None


def dedupe_key(payload: dict[str, Any], kind: str) -> str:
    items = payload.get("history_items") or []
    item_id = items[0].get("id") if items else ""
    return f"{payload.get('task_id')}:{kind}:{item_id}"


def match_account(accounts: list[dict[str, Any]], folder_id: str | None, list_id: str | None) -> dict[str, Any] | None:
    if not folder_id and not list_id:
        return None
    by_folder = [a for a in accounts if folder_id and str((a.get("integrations") or {}).get("clickupFolderId") or "") == str(folder_id)]
    if list_id:
        for a in accounts:
            if str((a.get("integrations") or {}).get("clickupListId") or "") == str(list_id):
                return a
    if not by_folder:
        return None
    parents = [a for a in by_folder if not a.get("parentId")]
    return (parents or by_folder)[0]


def internal_channel_name_from_shared(shared_name: str | None) -> str | None:
    if not shared_name or not _SHARED_PREFIX.match(shared_name):
        return None
    slug = _SHARED_PREFIX.sub("", shared_name).replace("_", "-").strip("-")
    return f"internal-{slug}" if slug else None


def _who(item: dict[str, Any] | None) -> str | None:
    user = (item or {}).get("user") or {}
    return user.get("username") or user.get("email")


def format_message(kind: str, task: dict[str, Any], item: dict[str, Any] | None) -> str:
    name = (task.get("name") or "Untitled task").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "¦")
    url = task.get("url") or f"https://app.clickup.com/t/{task.get('id')}"
    link = f"<{url}|{name}>"
    list_name = (task.get("list") or {}).get("name")
    who = _who(item)
    if kind == "created":
        who = who or ((task.get("creator") or {}).get("username"))
        line = f":new: *Task created:* {link}"
    else:
        line = f":white_check_mark: *Task completed:* {link}"
    meta = []
    if who:
        meta.append(f"by {who}")
    if list_name:
        meta.append(f"in {list_name}")
    assignees = [a.get("username") for a in task.get("assignees") or [] if a.get("username")]
    if kind == "created" and assignees:
        meta.append("assigned " + ", ".join(assignees))
    return line + (f"\n{' · '.join(meta)}" if meta else "")


# --- I/O --------------------------------------------------------------------

def _accounts(atlas: Any) -> list[dict[str, Any]]:
    with _cache.lock:
        if _cache.accounts and time.time() - _cache.accounts[0] < _ACCOUNTS_TTL:
            return _cache.accounts[1]
    accounts = atlas.get_all_accounts()
    with _cache.lock:
        _cache.accounts = (time.time(), accounts)
    return accounts


def _channel_index(slack: Any, *, refresh: bool = False) -> dict[str, str]:
    # Single-flight: conversations.list is Slack Tier 2 (~20/min). Concurrent
    # webhook deliveries on a cold cache each listing the whole workspace hit
    # 429s and stalled for minutes (seen live 2026-11-23) -- so one fetch at a
    # time, and everyone else waits for and reuses its result.
    with _cache.fetch_lock:
        with _cache.lock:
            if _cache.channels:
                age = time.time() - _cache.channels[0]
                # A forced refresh (lookup miss) is throttled: most clients have no
                # internal channel at all, and re-listing on every one of their
                # task events would burn Slack's rate limit.
                if age < (_CHANNELS_MIN_REFRESH if refresh else _CHANNELS_TTL):
                    return _cache.channels[1]
        # Keyed by NORMALIZED name (lowercase alnum only) so old-style
        # "internal-cambria_windows" and new-style "internal-c3-sewer-and-drains"
        # match the same way; oldest first so the newest channel wins a collision
        # (e.g. internal-sky_clean_air vs internal-sky-clean-air). Archived
        # channels are excluded server-side (~580 of the public ones), and 1000
        # per page keeps the whole workspace to about one call.
        rows = sorted(slack.list_channels(exclude_archived=True, limit=1000), key=lambda c: c.get("created") or 0)
        index = {_norm(c["name"]): c["id"] for c in rows if not c.get("is_archived")}
        with _cache.lock:
            _cache.channels = (time.time(), index)
        return index


def warm_channel_index() -> None:
    """Called once at startup on a background thread so the first webhook
    delivery after a deploy doesn't pay for the listing."""
    try:
        from app.integrations.slack import SlackClient

        _channel_index(SlackClient())
    except Exception:
        logger.exception("clickup notify: channel index warm-up failed")


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def resolve_internal_channel(account: dict[str, Any], slack: Any) -> str | None:
    """Atlas's internalSlackChannelId when set; else an EXACT normalized-name
    match of internal-<slug>, where slug is the shared channel's suffix or the
    company name. Exact only -- a substring match could land a client's task
    feed in another client's internal channel."""
    integrations = account.get("integrations") or {}
    for field in _INTERNAL_FIELDS:
        if integrations.get(field):
            return str(integrations[field])
    candidates = []
    shared_id = integrations.get("slackChannelId")
    derived = internal_channel_name_from_shared(slack.get_channel_name(shared_id) if shared_id else None)
    if derived:
        candidates.append(_norm(derived))
    if account.get("companyName"):
        candidates.append(_norm("internal-" + account["companyName"]))
    candidates = [c for c in candidates if c != "internal"]
    if not candidates:
        return None
    for refresh in (False, True):  # retry once: the channel may postdate the cached index
        index = _channel_index(slack, refresh=refresh)
        for c in candidates:
            if c in index:
                return index[c]
    return None


def process_event(
    payload: dict[str, Any],
    *,
    clickup_factory: Callable[[], Any] | None = None,
    atlas_factory: Callable[[], Any] | None = None,
    slack_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Returns a small outcome dict (also what gets logged)."""
    classified = classify_event(payload)
    if classified is None:
        return {"outcome": "ignored", "event": payload.get("event")}
    kind, item = classified

    key = dedupe_key(payload, kind)
    with _cache.lock:
        if key in _cache.seen:
            return {"outcome": "duplicate", "key": key}
        _cache.seen[key] = None
        while len(_cache.seen) > _DEDUPE_MAX:
            _cache.seen.popitem(last=False)

    if clickup_factory is None:
        from app.integrations.clickup import ClickUpClient as clickup_factory  # noqa: N813
    if atlas_factory is None:
        from app.integrations.atlas_client import AtlasClient as atlas_factory  # noqa: N813
    if slack_factory is None:
        from app.integrations.slack import SlackClient as slack_factory  # noqa: N813

    task_id = payload.get("task_id")
    task = clickup_factory().get_task(task_id)

    space_id = str((task.get("space") or {}).get("id") or "")
    if space_id and space_id != str(get_settings().clickup_notify_space_id):
        return {"outcome": "other_space", "task_id": task_id, "space_id": space_id}

    folder = task.get("folder") or {}
    folder_id = None if folder.get("hidden") else folder.get("id")
    list_id = (task.get("list") or {}).get("id")
    account = match_account(_accounts(atlas_factory()), folder_id, list_id)
    if account is None:
        logger.info("clickup notify: no Atlas account for task %s (folder %s, list %s)", task_id, folder_id, list_id)
        return {"outcome": "no_account", "task_id": task_id, "folder_id": folder_id}

    slack = slack_factory()
    channel_id = resolve_internal_channel(account, slack)
    if not channel_id:
        logger.warning("clickup notify: no internal channel for %s (task %s)", account.get("companyName"), task_id)
        return {"outcome": "no_internal_channel", "task_id": task_id, "account": account.get("companyName")}

    slack.post_message(channel_id, format_message(kind, task, item))
    return {"outcome": "posted", "kind": kind, "task_id": task_id, "account": account.get("companyName"), "channel": channel_id}
