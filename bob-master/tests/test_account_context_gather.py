from app.tasks.account_context_gather import extract_channel_client_name, gather_atlas_context, gather_rich_context


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

    def channel_history(self, channel_id):
        if self._raise_on_history:
            raise RuntimeError("slack down")
        return self._messages


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
    slack = _FakeSlack(messages=[{"text": "hey team, launching soon"}, {"text": "still waiting on assets"}])
    channels = [{"id": "C123", "name": "internal-acme-co"}, {"id": "C456", "name": "internal-beta-llc"}]

    result = gather_rich_context("Acme Co", None, clickup, slack, channels)

    assert "[Slack #internal-acme-co] hey team, launching soon" in result.context
    assert "[Slack #internal-acme-co] still waiting on assets" in result.context
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
            {"text": "hey team, real update here"},
        ]
    )
    channels = [{"id": "C123", "name": "internal-acme-co"}]

    result = gather_rich_context("Acme Co", None, clickup, slack, channels)

    assert result.context == ["[Slack #internal-acme-co] hey team, real update here"]
    assert result.slack_message_count == 1


def test_gather_rich_context_accepts_ambiguous_confidence_slack_matches():
    # Liberal on purpose (Bob, 2026-07-31): a wrong Slack channel just means
    # extra context, not a wrong account correlation, so "ambiguous" is good
    # enough here even though it isn't for ClickUp/retention board matching.
    clickup = _FakeClickUp()
    slack = _FakeSlack(messages=[{"text": "quick update on the account"}])
    channels = [{"id": "C1", "name": "internal-roof-city-pros"}]  # scores ~0.76, "ambiguous"

    result = gather_rich_context("Roof City Professionals", None, clickup, slack, channels)

    assert "[Slack #internal-roof-city-pros] quick update on the account" in result.context
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
            "list1": [{"id": "task1", "name": "Kickoff"}],
            "list2": [{"id": "task2", "name": "Meta campaign"}, {"id": "task3", "name": "No comments here"}],
        },
        comments_by_task={
            "task1": [{"comment_text": "waiting on domain access"}],
            "task2": [{"comment_text": "campaign live, watching CPA"}],
        },
    )
    slack = _FakeSlack(messages=[{"text": "quick check-in from the team"}])

    result = gather_atlas_context("folder1", "C0BEN1V1J0H", clickup, slack)

    assert "[ClickUp comment, task task1 (Kickoff)] waiting on domain access" in result.context
    assert "[ClickUp comment, task task2 (Meta campaign)] campaign live, watching CPA" in result.context
    assert result.clickup_comment_count == 2
    assert result.clickup_ok is True

    assert "[Slack #C0BEN1V1J0H] quick check-in from the team" in result.context
    assert result.slack_channel_matched == "C0BEN1V1J0H"
    assert result.slack_match_confidence == "atlas_exact_id"
    assert result.slack_match_score == 1.0
    assert result.slack_ok is True


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
