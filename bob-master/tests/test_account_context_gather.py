from datetime import datetime, timedelta, timezone

from app.tasks.account_context_gather import (
    _slack_sender_name,
    business_hours_cutoff,
    extract_channel_client_name,
    gather_atlas_context,
    gather_rich_context,
)


def _recent_ms(days_ago: float = 0) -> str:
    """Epoch-ms string within the 7-day context window, matching real ClickUp
    date_updated/date field format."""
    return str(int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp() * 1000))


def _stale_ms(days_ago: float = 30) -> str:
    return str(int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp() * 1000))


def test_extract_channel_client_name_strips_internal_prefix():
    assert extract_channel_client_name("internal-acme-co") == "acme-co"
    assert extract_channel_client_name("INTERNAL-Acme-Co") == "Acme-Co"
    # Client-facing twin channel is a different prefix -- must not be stripped,
    # so it never accidentally matches as if it were the internal channel.
    assert extract_channel_client_name("advancedmarketers_x_acme-co") == "advancedmarketers_x_acme-co"


class _FakeClickUp:
    def __init__(self, subtasks=None, comments_by_task=None, raise_on_task=False):
        self._subtasks = subtasks or []
        self._comments_by_task = comments_by_task or {}
        self._raise_on_task = raise_on_task

    def get_task_with_subtasks(self, task_id):
        if self._raise_on_task:
            raise RuntimeError("clickup down")
        return {"id": task_id, "subtasks": self._subtasks}

    def get_task_comments(self, task_id):
        return self._comments_by_task.get(task_id, [])


class _FakeSlack:
    def __init__(self, messages=None, raise_on_history=False):
        self._messages = messages or []
        self._raise_on_history = raise_on_history
        self.requested_oldest_ts = None

    def channel_history(self, channel_id, oldest_ts=None):
        self.requested_oldest_ts = oldest_ts
        if self._raise_on_history:
            raise RuntimeError("slack down")
        return self._messages

    def get_user_display_name(self, user_id):
        return {"U1": "Jordan"}.get(user_id, user_id)


def test_slack_sender_name_resolves_a_user_id():
    slack = _FakeSlack()
    assert _slack_sender_name(slack, {"text": "hi", "user": "U1"}) == "Jordan"


def test_slack_sender_name_uses_the_bot_username_when_theres_no_user_id():
    # Bot/webhook messages carry their own "username" directly -- there's no
    # Slack user ID to resolve, and users.info doesn't accept a bot_id.
    slack = _FakeSlack()
    assert _slack_sender_name(slack, {"text": "deploy succeeded", "username": "CI Bot"}) == "CI Bot"


def test_slack_sender_name_falls_back_to_unknown_for_an_unattributed_message():
    slack = _FakeSlack()
    assert _slack_sender_name(slack, {"text": "mystery message"}) == "Unknown"


def test_gather_rich_context_pulls_card_and_subtask_comments():
    clickup = _FakeClickUp(
        subtasks=[{"id": "sub1"}, {"id": "sub2"}],
        comments_by_task={
            "card1": [{"comment_text": "waiting on access"}],
            "sub1": [{"comment_text": "[CLIENT] hasn't sent logo yet"}],
            "sub2": [],
        },
    )
    slack = _FakeSlack()

    result = gather_rich_context("Acme Co", "card1", clickup, slack, slack_channels=[])

    assert "[ClickUp comment, task card1] waiting on access" in result.context
    assert "[ClickUp comment, task sub1] [CLIENT] hasn't sent logo yet" in result.context
    assert result.clickup_ok is True
    assert result.clickup_comment_count == 2
    assert result.clickup_error is None


def test_gather_rich_context_pulls_full_slack_channel_history_when_matched():
    clickup = _FakeClickUp()
    slack = _FakeSlack(messages=[
        {"text": "hey team, launching soon", "user": "U1"},
        {"text": "still waiting on assets", "user": "U1"},
    ])
    channels = [{"id": "C123", "name": "internal-acme-co"}, {"id": "C456", "name": "internal-beta-llc"}]

    result = gather_rich_context("Acme Co", None, clickup, slack, channels)

    assert "[Slack #internal-acme-co] Jordan: hey team, launching soon" in result.context
    assert "[Slack #internal-acme-co] Jordan: still waiting on assets" in result.context
    assert result.slack_channel_matched == "internal-acme-co"
    assert result.slack_match_confidence == "exact"
    assert result.slack_match_score == 1.0
    assert result.slack_ok is True
    assert result.slack_message_count == 2
    assert result.slack_error is None


def test_gather_rich_context_filters_out_slack_system_message_noise():
    # Real bug (2026-07-31): a busy channel's "has joined the channel" system
    # messages bloated the LLM input enough to blow a batch's token budget,
    # contributing to raw transcripts leaking into the dashboard.
    clickup = _FakeClickUp()
    slack = _FakeSlack(
        messages=[
            {"text": "<@U0BLXKX8LS1> has joined the channel", "subtype": "channel_join"},
            {"text": "set the channel topic", "subtype": "channel_topic"},
            {"text": "hey team, real update here", "user": "U1"},
        ]
    )
    channels = [{"id": "C123", "name": "internal-acme-co"}]

    result = gather_rich_context("Acme Co", None, clickup, slack, channels)

    assert result.context == ["[Slack #internal-acme-co] Jordan: hey team, real update here"]
    assert result.slack_message_count == 1


def test_gather_rich_context_accepts_ambiguous_confidence_slack_matches():
    # Liberal on purpose (Bob, 2026-07-31): a wrong Slack channel just means
    # extra context, not a wrong account correlation, so "ambiguous" is good
    # enough here even though it isn't for ClickUp/retention board matching.
    clickup = _FakeClickUp()
    slack = _FakeSlack(messages=[{"text": "quick update on the account", "user": "U1"}])
    channels = [{"id": "C1", "name": "internal-roof-city-pros"}]  # scores ~0.76, "ambiguous"

    result = gather_rich_context("Roof City Professionals", None, clickup, slack, channels)

    assert "[Slack #internal-roof-city-pros] Jordan: quick update on the account" in result.context
    assert result.slack_channel_matched == "internal-roof-city-pros"
    assert result.slack_match_confidence == "ambiguous"
    assert result.slack_match_score is not None and 0.72 <= result.slack_match_score < 0.85


def test_gather_rich_context_skips_slack_when_no_confident_channel_match():
    clickup = _FakeClickUp()
    slack = _FakeSlack(messages=[{"text": "should never appear"}])
    channels = [{"id": "C999", "name": "internal-totally-unrelated-business"}]

    result = gather_rich_context("Acme Co", None, clickup, slack, channels)

    assert result.context == []
    assert result.slack_channel_matched is None
    assert result.slack_match_confidence is None
    assert result.slack_ok is True
    assert result.slack_message_count == 0


def test_gather_rich_context_is_resilient_to_clickup_failure():
    clickup = _FakeClickUp(raise_on_task=True)
    slack = _FakeSlack()

    result = gather_rich_context("Acme Co", "card1", clickup, slack, slack_channels=[])

    assert any("ClickUp context fetch failed" in c for c in result.context)
    assert result.clickup_ok is False
    assert "clickup down" in result.clickup_error
    assert result.clickup_comment_count == 0


def test_gather_rich_context_is_resilient_to_slack_failure():
    clickup = _FakeClickUp()
    slack = _FakeSlack(raise_on_history=True)
    channels = [{"id": "C123", "name": "internal-acme-co"}]

    result = gather_rich_context("Acme Co", None, clickup, slack, channels)

    assert any("Slack context fetch failed" in c for c in result.context)
    assert result.slack_ok is False
    assert "slack down" in result.slack_error
    assert result.slack_channel_matched == "internal-acme-co"  # matched fine, the history call is what failed
    assert result.slack_match_confidence == "exact"
    assert result.slack_message_count == 0


def test_gather_rich_context_returns_empty_without_card_id_or_slack_match():
    clickup = _FakeClickUp()
    slack = _FakeSlack()

    result = gather_rich_context("Acme Co", None, clickup, slack, slack_channels=[])
    assert result.context == []
    assert result.clickup_ok is True
    assert result.slack_ok is True


class _FakeClickUpFolder:
    """Real shape confirmed against a live folder 2026-08-04: {"lists": [...]},
    each list's tasks via the existing get_list_tasks shape."""

    def __init__(self, lists=None, tasks_by_list=None, comments_by_task=None, raise_on_folder=False):
        self._lists = lists or []
        self._tasks_by_list = tasks_by_list or {}
        self._comments_by_task = comments_by_task or {}
        self._raise_on_folder = raise_on_folder

    def get_folder_lists(self, folder_id):
        if self._raise_on_folder:
            raise RuntimeError("clickup folder down")
        return self._lists

    def get_list_tasks(self, list_id, include_closed=True, page=0):
        return {"tasks": self._tasks_by_list.get(list_id, [])}

    def get_task_comments(self, task_id):
        return self._comments_by_task.get(task_id, [])


def test_gather_atlas_context_walks_folder_lists_tasks_and_comments():
    clickup = _FakeClickUpFolder(
        lists=[{"id": "list1", "name": "TODO"}, {"id": "list2", "name": "Weekly CM"}],
        tasks_by_list={
            "list1": [{"id": "task1", "name": "Kickoff", "date_updated": _recent_ms()}],
            "list2": [
                {"id": "task2", "name": "Meta campaign", "date_updated": _recent_ms(2)},
                {"id": "task3", "name": "No comments here", "date_updated": _recent_ms()},
            ],
        },
        comments_by_task={
            "task1": [{"comment_text": "waiting on domain access", "date": _recent_ms()}],
            "task2": [{"comment_text": "campaign live, watching CPA", "date": _recent_ms(2)}],
        },
    )
    slack = _FakeSlack(messages=[{"text": "quick check-in from the team", "user": "U1"}])

    result = gather_atlas_context("folder1", "C0BEN1V1J0H", clickup, slack)

    assert "[ClickUp comment, task task1 (Kickoff)] waiting on domain access" in result.context
    assert "[ClickUp comment, task task2 (Meta campaign)] campaign live, watching CPA" in result.context
    assert result.clickup_comment_count == 2
    assert result.clickup_ok is True

    assert "[Slack #C0BEN1V1J0H] Jordan: quick check-in from the team" in result.context
    assert result.slack_channel_matched == "C0BEN1V1J0H"
    assert result.slack_match_confidence == "atlas_exact_id"
    assert result.slack_match_score == 1.0
    assert result.slack_ok is True


def test_gather_atlas_context_skips_stale_tasks_without_fetching_their_comments():
    # The actual point of the window: a task with no recent activity never
    # gets its comments fetched at all -- this is what relieves ClickUp's
    # rate limit, not just what shrinks the final context.
    fetched_comment_calls = []

    class _TrackingClickUp(_FakeClickUpFolder):
        def get_task_comments(self, task_id):
            fetched_comment_calls.append(task_id)
            return super().get_task_comments(task_id)

    clickup = _TrackingClickUp(
        lists=[{"id": "list1"}],
        tasks_by_list={
            "list1": [
                {"id": "fresh-task", "name": "Fresh", "date_updated": _recent_ms()},
                {"id": "stale-task", "name": "Stale", "date_updated": _stale_ms()},
            ],
        },
        comments_by_task={
            "fresh-task": [{"comment_text": "recent activity", "date": _recent_ms()}],
            "stale-task": [{"comment_text": "old activity, should never be seen", "date": _stale_ms()}],
        },
    )
    slack = _FakeSlack()

    result = gather_atlas_context("folder1", None, clickup, slack)

    assert fetched_comment_calls == ["fresh-task"]  # stale-task's comments were never even requested
    assert "recent activity" in " ".join(result.context)
    assert "should never be seen" not in " ".join(result.context)


def test_gather_atlas_context_filters_stale_comments_on_an_otherwise_recent_task():
    # A task touched recently can still carry old comments mixed with new
    # ones -- only the ones inside the window belong in the LLM's input.
    clickup = _FakeClickUpFolder(
        lists=[{"id": "list1"}],
        tasks_by_list={"list1": [{"id": "task1", "name": "Ongoing", "date_updated": _recent_ms()}]},
        comments_by_task={
            "task1": [
                {"comment_text": "brand new note", "date": _recent_ms()},
                {"comment_text": "ancient note", "date": _stale_ms()},
            ]
        },
    )
    slack = _FakeSlack()

    result = gather_atlas_context("folder1", None, clickup, slack)

    assert any("brand new note" in c for c in result.context)
    assert not any("ancient note" in c for c in result.context)
    assert result.clickup_comment_count == 1


def test_gather_atlas_context_passes_a_7_day_oldest_ts_to_slack():
    clickup = _FakeClickUpFolder(lists=[])
    slack = _FakeSlack(messages=[])

    gather_atlas_context(None, "C123", clickup, slack)

    assert slack.requested_oldest_ts is not None
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
    # within a couple seconds of the expected cutoff -- not exact, just sane
    assert abs(float(slack.requested_oldest_ts) - seven_days_ago) < 5


def test_gather_atlas_context_window_days_override_changes_the_slack_cutoff():
    clickup = _FakeClickUpFolder(lists=[])
    slack = _FakeSlack(messages=[])

    gather_atlas_context(None, "C123", clickup, slack, window_days=10)

    ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    assert abs(float(slack.requested_oldest_ts) - ten_days_ago) < 5


def test_gather_atlas_context_no_matching_at_all_just_uses_the_ids_directly():
    # The whole point: no account_name, no slack_channels list, no fuzzy match call.
    clickup = _FakeClickUpFolder(lists=[])
    slack = _FakeSlack(messages=[])

    result = gather_atlas_context(None, None, clickup, slack)

    assert result.context == []
    assert result.clickup_ok is True
    assert result.slack_ok is True
    assert result.slack_channel_matched is None


def test_gather_atlas_context_is_resilient_to_folder_fetch_failure():
    clickup = _FakeClickUpFolder(raise_on_folder=True)
    slack = _FakeSlack()

    result = gather_atlas_context("folder1", None, clickup, slack)

    assert any("ClickUp context fetch failed for folder folder1" in c for c in result.context)
    assert result.clickup_ok is False
    assert "clickup folder down" in result.clickup_error


def test_business_hours_cutoff_monday_morning_reaches_back_to_thursday():
    # The actual motivating case (Bob, 2026-09-21): a flat 48-calendar-hour
    # window checked Monday morning would only reach Saturday, missing all
    # of Friday's real work. 2026-09-21 is a real Monday.
    now = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    cutoff = business_hours_cutoff(48, now=now)
    assert cutoff == datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
    assert cutoff.strftime("%A") == "Thursday"


def test_business_hours_cutoff_with_no_weekend_in_range_is_a_flat_subtraction():
    now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)  # Wednesday
    cutoff = business_hours_cutoff(24, now=now)
    assert cutoff == datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)  # Tuesday, exactly 24h back


def test_business_hours_cutoff_starting_on_a_weekend_skips_weekend_hours_too():
    now = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)  # Saturday
    cutoff = business_hours_cutoff(8, now=now)
    assert cutoff == datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc)  # Friday 16:00
    assert cutoff.strftime("%A") == "Friday"


def test_business_hours_cutoff_defaults_now_to_the_real_current_time():
    before = datetime.now(timezone.utc)
    cutoff = business_hours_cutoff(0)
    after = datetime.now(timezone.utc)
    assert before <= cutoff <= after


def test_gather_atlas_context_recent_activity_hours_none_by_default_leaves_it_empty():
    # Every existing caller (e.g. daily_go_live_audit.py) that doesn't pass
    # recent_activity_hours must see zero behavior change.
    clickup = _FakeClickUpFolder(
        lists=[{"id": "list1"}],
        tasks_by_list={"list1": [{"id": "task1", "name": "Ongoing", "date_updated": _recent_ms()}]},
        comments_by_task={"task1": [{"comment_text": "brand new note", "date": _recent_ms()}]},
    )
    slack = _FakeSlack()

    result = gather_atlas_context("folder1", None, clickup, slack)

    assert result.recent_clickup_activity == []
    assert any("brand new note" in c for c in result.context)  # unaffected


def test_gather_atlas_context_recent_activity_hours_is_a_subset_never_a_narrower_llm_window():
    # Real requirement (Bob, 2026-09-21): "I still want the full LLM blend
    # window to consider the full timerange of all platforms" -- a comment
    # inside the wide 7-day window but OUTSIDE the tight recent-activity
    # window must still reach the LLM's `context`, just not the display-only
    # recent_clickup_activity list.
    within_recent = _recent_ms(0)
    # 5 real calendar days ago: still inside the 7-day gather window, but
    # safely outside ANY 48-weekday-hour cutoff regardless of what day this
    # suite happens to run on -- worst case (a Monday "now"), 48 weekday
    # hours only reaches back 4 calendar days (through Thu-Fri, skipping the
    # weekend), so 3 days would sometimes land wrongly ON the inside.
    within_wide_only = _recent_ms(5)
    clickup = _FakeClickUpFolder(
        lists=[{"id": "list1"}],
        tasks_by_list={"list1": [{"id": "task1", "name": "Ongoing", "date_updated": within_recent}]},
        comments_by_task={
            "task1": [
                {"comment_text": "very fresh note", "date": within_recent},
                {"comment_text": "three days old note", "date": within_wide_only},
            ]
        },
    )
    slack = _FakeSlack()

    result = gather_atlas_context("folder1", None, clickup, slack, recent_activity_hours=48)

    # Both comments still reach the LLM's context -- the wide window is untouched.
    assert any("very fresh note" in c for c in result.context)
    assert any("three days old note" in c for c in result.context)
    assert result.clickup_comment_count == 2

    # Only the genuinely-recent one shows up in the display-only list.
    recent_texts = [e["text"] for e in result.recent_clickup_activity]
    assert "very fresh note" in recent_texts
    assert "three days old note" not in recent_texts


def test_gather_atlas_context_recent_activity_entries_carry_task_identity():
    clickup = _FakeClickUpFolder(
        lists=[{"id": "list1"}],
        tasks_by_list={"list1": [{"id": "task1", "name": "Fix redirect", "date_updated": _recent_ms()}]},
        comments_by_task={"task1": [{"comment_text": "deployed the fix", "date": _recent_ms()}]},
    )
    slack = _FakeSlack()

    result = gather_atlas_context("folder1", None, clickup, slack, recent_activity_hours=48)

    assert len(result.recent_clickup_activity) == 1
    entry = result.recent_clickup_activity[0]
    assert entry["task_id"] == "task1"
    assert entry["task_name"] == "Fix redirect"
    assert entry["text"] == "deployed the fix"
    assert entry["date_ms"] == _recent_ms()
