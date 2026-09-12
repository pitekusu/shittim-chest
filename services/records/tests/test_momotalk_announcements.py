"""Publication and durable Discord deduplication; only synthetic, offline messages."""

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import httpx
import pytest
from shittim_chest.adapters.discord.announcements import AnnouncementError, AnnouncementTarget
from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.application.discord import (
    DiscordBotSlot,
    DiscordIdentityConfig,
    DiscordRuntimeConfig,
)
from shittim_chest.config.status_publisher import MODERATOR_TOKEN_PARAMETER
from tests.test_momotalk import ROOM_ID, WEEK

from shittim_records import momotalk_announcements as notice
from shittim_records import momotalk_handlers as handlers
from shittim_records.momotalk import PARTICIPANTS, ConversationPlan, Room, SavedMessage, Turn
from shittim_records.momotalk_adapters import DynamoMomotalkStore


def ready_room() -> Room:
    plan = ConversationPlan(
        summary="架空の雑談",
        images=[],
        turns=[
            Turn(participant=PARTICIPANTS[index], topic="架空の話題")
            for index in (0, 1, 0, 2, 1, 2, 0, 2, 1)
        ],
    )
    return Room(
        week_id=WEEK.week_id,
        room_id=ROOM_ID,
        display_name="テスト利用者",
        question_count=0,
        state="ready",
        plan=plan,
        messages=[
            SavedMessage(participant=turn.participant, text="架空の発言") for turn in plan.turns
        ],
    )


class Receipts:
    def __init__(self):
        self.receipts = {}
        self.ready = True
        self.claimed_elsewhere = False

    def load(self, week):
        return self.receipts.get(week)

    def readable(self, week, now):
        return self.ready and now >= WEEK.publish_at

    def reserve(self, week, fingerprint, now):
        if self.claimed_elsewhere or week in self.receipts:
            return False
        self.receipts[week] = notice.Receipt(fingerprint, "attempted")
        return True

    def mark_sent(self, week, fingerprint, now):
        self.receipts[week] = notice.Receipt(fingerprint, "sent")


@pytest.fixture
def runtime():
    return DiscordRuntimeConfig(
        guild_id="1",
        allowed_channel_ids=frozenset({"2"}),
        farewell_channel_id="2",
        identities=tuple(
            DiscordIdentityConfig(slot, str(i)) for i, slot in enumerate(DiscordBotSlot, 11)
        ),
        schema_version="2",
    )


@pytest.fixture
def api(monkeypatch):
    target = AnnouncementTarget("1", "2", "test-channel", "99", "test-bot")
    resolve = Mock(return_value=target)
    monkeypatch.setattr(notice, "resolve_target_channel", resolve)
    state: dict[str, Any] = {
        "history": [],
        "posted": None,
        "timeout": False,
        "bad_verification": False,
    }
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "POST":
            message = {
                **json.loads(request.content),
                "id": "9",
                "channel_id": "2",
                "author": {"id": "99"},
                "embeds": [],
            }
            state["history"] = [message]
            state["posted"] = message
            if state["timeout"]:
                raise httpx.ReadTimeout("synthetic provider detail must not be logged")
            return httpx.Response(200, json=message)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=state["history"])
        message = state["posted"]
        if state["bad_verification"]:
            message = {**message, "author": {"id": "98"}}
        return httpx.Response(200, json=message)

    with httpx.Client(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        yield client, state, calls, resolve


def publish(store, client, runtime, *, now=WEEK.publish_at):
    return notice.publish_announcement(
        cast(Any, store),
        client,
        runtime,
        week_id=WEEK.week_id,
        now=now,
        public_hostname="shittim.pitekusu.dev",
    )


def test_publish_once_with_url_and_shared_announcement_safety(api, runtime):
    client, _state, calls, resolve = api
    store = Receipts()
    assert publish(store, client, runtime) == "posted_and_verified"
    assert publish(store, client, runtime) == "already_posted"
    assert [call.method for call in calls] == ["GET", "POST", "GET"]
    resolve.assert_called_once_with(client, runtime, channel_id=runtime.farewell_channel_id)
    payload = json.loads(calls[1].content)
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["enforce_nonce"] is True
    assert payload["nonce"] == notice.announcement_nonce(f"momotalk-{WEEK.week_id}")
    assert "2026年9月13日公開分" in payload["content"]
    assert "https://shittim.pitekusu.dev/momotalk" in payload["content"]
    assert "\n\n" in payload["content"]
    assert store.load(WEEK.week_id).state == "sent"
    # Discovery can recover an existing notice without issuing a second POST.
    assert publish(Receipts(), client, runtime) == "already_posted"
    assert len([call for call in calls if call.method == "POST"]) == 1


@pytest.mark.parametrize("ready,offset", [(True, -1), (False, 0), (False, 3600)])
def test_no_early_or_unreadable_announcement(api, runtime, ready, offset):
    client, _state, calls, resolve = api
    store = Receipts()
    store.ready = ready
    assert (
        publish(store, client, runtime, now=WEEK.publish_at + timedelta(seconds=offset))
        == "not_ready"
    )
    assert calls == []
    resolve.assert_not_called()
    assert not store.receipts


@pytest.mark.parametrize("corruption", ["flag_only", "incomplete", "key", "version", "preparing"])
def test_invalid_checkpoint_after_ready_room_never_announces(api, runtime, corruption):
    client, _state, calls, resolve = api
    database = Mock()
    database.get_item.side_effect = [
        {},
        {"Item": marshal_item({"week": WEEK.model_dump(mode="json")})},
    ]
    store = DynamoMomotalkStore(database, "statistics")
    valid_item = store._item(ready_room())
    item = store._item(ready_room().model_copy(update={"room_id": "b" * 43}))
    if corruption == "flag_only":
        item["payload"] = {"state": "ready"}
    elif corruption == "incomplete":
        item["payload"]["messages"] = []
    elif corruption == "key":
        item["SK"] = "c" * 43
    elif corruption == "version":
        item["version"] += 1
    else:
        item["payload"] = {"state": "preparing"}
    # A valid first room must not hide a broken row, even on a later query page.
    items = [marshal_item(valid_item), marshal_item(item)]
    database.get_paginator.return_value.paginate.return_value = (
        [{"Items": items[:1]}, {"Items": items[1:]}]
        if corruption in ("key", "version")
        else [{"Items": items}]
    )
    with pytest.raises(AnnouncementError, match="momotalk_publication_invalid"):
        publish(notice.DynamoMomotalkAnnouncements(database, "statistics"), client, runtime)
    assert calls == []
    resolve.assert_not_called()
    database.put_item.assert_not_called()
    database.update_item.assert_not_called()


def test_readable_finds_valid_conversation_on_later_page():
    database = Mock()
    database.get_item.return_value = {"Item": marshal_item({"week": WEEK.model_dump(mode="json")})}
    store = DynamoMomotalkStore(database, "statistics")
    item = store._item(ready_room())
    preparing = store._item(
        ready_room().model_copy(update={"room_id": "b" * 43, "state": "preparing"})
    )
    database.get_paginator.return_value.paginate.return_value = [
        {"Items": []},
        {"Items": [marshal_item(preparing), marshal_item(item)]},
    ]
    assert notice.DynamoMomotalkAnnouncements(database, "statistics").readable(
        WEEK.week_id, WEEK.publish_at
    )


@pytest.mark.parametrize("visible_in_history", [True, False])
def test_unknown_post_result_is_reconciled_but_never_blindly_reposted(
    api, runtime, visible_in_history
):
    client, state, calls, _resolve = api
    store = Receipts()
    state["timeout"] = True
    with pytest.raises(httpx.ReadTimeout):
        publish(store, client, runtime)
    assert store.load(WEEK.week_id).state == "attempted"
    if visible_in_history:
        assert publish(store, client, runtime) == "already_posted"
        assert store.load(WEEK.week_id).state == "sent"
    else:
        state["history"] = []
        with pytest.raises(AnnouncementError, match="previous_send_attempt_exists"):
            publish(store, client, runtime)
    assert len([call for call in calls if call.method == "POST"]) == 1


def test_exclusive_claim_and_failed_verification(api, runtime):
    client, state, calls, _resolve = api
    store = Receipts()
    store.claimed_elsewhere = True
    assert publish(store, client, runtime) == "in_progress"
    assert all(call.method == "GET" for call in calls)
    store.claimed_elsewhere = False
    state["bad_verification"] = True
    with pytest.raises(AnnouncementError, match="post_verification_failed"):
        publish(store, client, runtime)
    assert store.load(WEEK.week_id).state == "attempted"


def test_schedule_and_late_generation_event_week():
    event = {
        "source": "aws.events",
        "detail-type": "Scheduled Event",
        "time": WEEK.publish_at.isoformat(),
    }
    assert notice.event_week(event, WEEK.publish_at + timedelta(minutes=2)) == WEEK.week_id
    assert (
        notice.event_week(
            {"source": "shittim.momotalk", "weekId": str(WEEK.week_id)}, WEEK.publish_at
        )
        == WEEK.week_id
    )
    for invalid in (
        {**event, "time": WEEK.period_end.isoformat()},
        {**event, "time": WEEK.publish_at.replace(tzinfo=None).isoformat()},
        {"source": "shittim.momotalk", "weekId": str(WEEK.week_id), "content": "untrusted"},
    ):
        with pytest.raises(AnnouncementError):
            notice.event_week(invalid, WEEK.publish_at)


@pytest.mark.parametrize(
    "state,offset,expected",
    [("ready", -1, False), ("preparing", 1, False), ("failed", 1, False), ("ready", 1, True)],
)
def test_worker_dispatches_ready_conversation_without_waiting_for_images(
    monkeypatch, state, offset, expected
):
    store = Mock()
    store.get_week.return_value = {"week": WEEK.model_dump(mode="json")}
    store.get_room.return_value = SimpleNamespace(state=state, complete=False)
    client = Mock()
    client.invoke.return_value = {"StatusCode": 202}
    monkeypatch.setattr(handlers.boto3, "client", Mock(return_value=client))
    monkeypatch.setenv("MOMOTALK_ANNOUNCEMENT_FUNCTION_NAME", "test-announcement")
    handlers.announce_if_readable(
        store,
        handlers.Job(weekId=str(WEEK.week_id), roomId=ROOM_ID),
        now=WEEK.publish_at + timedelta(seconds=offset),
    )
    assert client.invoke.called == expected
    if expected:
        arguments = client.invoke.call_args.kwargs
        assert arguments["FunctionName"] == "test-announcement"
        assert arguments["InvocationType"] == "Event"
        assert json.loads(arguments["Payload"]) == {
            "source": "shittim.momotalk",
            "weekId": str(WEEK.week_id),
        }


def test_dispatch_failure_retries_delivery_without_failing_saved_conversation(monkeypatch, caplog):
    service = Mock()
    service.run.return_value = True
    generator = Mock()
    monkeypatch.setattr(handlers, "_components", lambda: (None,) * 6)
    monkeypatch.setattr(handlers, "OpenAIMomotalkGenerator", Mock(return_value=generator))
    monkeypatch.setattr(handlers, "MomotalkGenerationService", Mock(return_value=service))
    monkeypatch.setattr(
        handlers, "announce_if_readable", Mock(side_effect=RuntimeError("synthetic-private-value"))
    )
    event = {
        "Records": [
            {
                "body": json.dumps({"weekId": str(WEEK.week_id), "roomId": ROOM_ID}),
                "messageId": "test-job",
            }
        ]
    }
    assert handlers.worker_handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "test-job"}]
    }
    generator.close.assert_called_once()
    assert "momotalk_announcement_dispatch_failed" in caplog.text
    assert "synthetic-private-value" not in caplog.text


@pytest.mark.parametrize("version", ["v0004", "v0003"])
def test_runtime_version_verified_before_loading_token(runtime, version):
    runtime_name = "/shittim-chest/production/runtime/v0004"
    payload = {
        "schema_version": "2",
        "config_version": version,
        "guild_id": runtime.guild_id,
        "allowed_channel_ids": sorted(runtime.allowed_channel_ids),
        "farewell_channel_id": runtime.farewell_channel_id,
        "identities": [
            {"slot": item.slot.value, "application_id": item.application_id}
            for item in runtime.identities
        ],
    }
    client = Mock()
    client.get_parameter.side_effect = [
        {"Parameter": {"Name": runtime_name, "Value": json.dumps(payload)}},
        {"Parameter": {"Name": MODERATOR_TOKEN_PARAMETER, "Value": "synthetic-token"}},
    ]
    if version == "v0004":
        assert notice.load_discord_config(client, runtime_name) == (runtime, "synthetic-token")
        assert client.get_parameter.call_count == 2
    else:
        with pytest.raises(AnnouncementError, match="unexpected_runtime_version"):
            notice.load_discord_config(client, runtime_name)
        assert client.get_parameter.call_count == 1
