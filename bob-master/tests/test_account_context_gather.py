from app.tasks.account_context_gather import extract_channel_client_name, gather_rich_context


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
    assert result.slack_ok is True
    assert result.slack_message_count == 2
    assert result.slack_error is None


def test_gather_rich_context_skips_slack_when_no_confident_channel_match():
    clickup = _FakeClickUp()
    slack = _FakeSlack(messages=[{"text": "should never appear"}])
    channels = [{"id": "C999", "name": "internal-totally-unrelated-business"}]

    result = gather_rich_context("Acme Co", None, clickup, slack, channels)

    assert result.context == []
    assert result.slack_channel_matched is None
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
    assert result.slack_message_count == 0


def test_gather_rich_context_returns_empty_without_card_id_or_slack_match():
    clickup = _FakeClickUp()
    slack = _FakeSlack()

    result = gather_rich_context("Acme Co", None, clickup, slack, slack_channels=[])
    assert result.context == []
    assert result.clickup_ok is True
    assert result.slack_ok is True
