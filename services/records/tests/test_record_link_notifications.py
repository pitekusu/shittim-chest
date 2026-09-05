"""Focused record-link delivery tests without AWS or Discord network access."""

from __future__ import annotations

from dataclasses import replace

import pytest
from shittim_chest.application.status_publication import DiscordStatusMessage
from tests.factories import NOW, completed_snapshot, presentation

from shittim_records.archive import project_completed_debate
from shittim_records.record_link_notifications import (
    RecordLinkNotificationReceipt,
    RecordLinkNotificationService,
    RecordLinkNotificationState,
)

HMAC_KEY = b"records-test-key-that-is-longer-than-32-bytes"
BOT_ID = "9" * 18
MESSAGE_ID = "8" * 18
GUILD_ID = "1" * 18
CHANNEL_ID = "2" * 18
STARTER_MESSAGE_ID = "3" * 18
THREAD_ID = "4" * 18


class FakeStore:
    def __init__(self, receipt: RecordLinkNotificationReceipt | None) -> None:
        self.receipt = receipt
        self.attempted = 0
        self.sent = 0

    def load(
        self,
        *,
        record_id: str,
        source_fingerprint: str,
    ) -> RecordLinkNotificationReceipt | None:
        if self.receipt is None:
            return None
        assert self.receipt.record_id == record_id
        assert self.receipt.source_fingerprint == source_fingerprint
        return self.receipt

    def mark_attempted(
        self,
        *,
        record_id: str,
        source_fingerprint: str,
        at: object,
    ) -> None:
        assert self.receipt is not None
        assert self.receipt.record_id == record_id
        assert self.receipt.source_fingerprint == source_fingerprint
        assert at == NOW
        self.attempted += 1
        self.receipt = replace(self.receipt, attempted=True)

    def mark_sent(
        self,
        *,
        record_id: str,
        source_fingerprint: str,
        at: object,
    ) -> None:
        assert self.receipt is not None
        assert self.receipt.record_id == record_id
        assert self.receipt.source_fingerprint == source_fingerprint
        assert at == NOW
        self.sent += 1
        self.receipt = replace(self.receipt, state=RecordLinkNotificationState.SENT)


class FakeGateway:
    def __init__(self, *, existing: bool = False, fail_create: bool = False) -> None:
        self.existing = existing
        self.fail_create = fail_create
        self.find_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []

    async def current_bot_user_id(self) -> str:
        return BOT_ID

    async def find_by_nonce(self, **kwargs: object) -> DiscordStatusMessage | None:
        self.find_calls.append(kwargs)
        if not self.existing:
            return None
        return self._message(kwargs)

    async def create_message(self, **kwargs: object) -> DiscordStatusMessage:
        self.create_calls.append(kwargs)
        if self.fail_create:
            raise RuntimeError("Discord unavailable")
        return self._message(kwargs)

    async def fetch_message(self, **_kwargs: object) -> DiscordStatusMessage:
        raise AssertionError("record-link delivery does not fetch by message ID")

    async def edit_message(self, **_kwargs: object) -> DiscordStatusMessage:
        raise AssertionError("record-link delivery does not edit messages")

    @staticmethod
    def _message(kwargs: dict[str, object]) -> DiscordStatusMessage:
        return DiscordStatusMessage(
            message_id=MESSAGE_ID,
            channel_id=str(kwargs["channel_id"]),
            author_id=BOT_ID,
            content=str(kwargs.get("content") or EXPECTED_CONTENT),
            nonce=str(kwargs["nonce"]),
        )


class FakeGatewayFactory:
    def __init__(self, gateway: FakeGateway) -> None:
        self.gateway = gateway

    async def create(self, snapshot: object) -> FakeGateway:
        assert snapshot is SNAPSHOT
        return self.gateway


SNAPSHOT = replace(
    completed_snapshot(),
    guild_id=GUILD_ID,
    channel_id=CHANNEL_ID,
    starter_message_id=STARTER_MESSAGE_ID,
    thread_id=THREAD_ID,
)
PROJECTION = project_completed_debate(
    SNAPSHOT,
    identity_hmac_key=HMAC_KEY,
    presentation=presentation(),
    projected_at=NOW,
)
EXPECTED_CONTENT = (
    "議論結果はこちらからも確認できます。\n"
    f"[Webで議論結果を見る](https://shittim.pitekusu.dev/records/{PROJECTION.record_id})"
)


def receipt(*, attempted: bool = False) -> RecordLinkNotificationReceipt:
    return RecordLinkNotificationReceipt(
        record_id=PROJECTION.record_id,
        source_fingerprint=PROJECTION.source_fingerprint,
        state=RecordLinkNotificationState.PENDING,
        attempted=attempted,
    )


def service(store: FakeStore, gateway: FakeGateway) -> RecordLinkNotificationService:
    return RecordLinkNotificationService(
        store=store,
        gateway_factory=FakeGatewayFactory(gateway),
        public_hostname="shittim.pitekusu.dev",
    )


def publish(value: RecordLinkNotificationService) -> None:
    value.publish(
        snapshot=SNAPSHOT,
        record_id=PROJECTION.record_id,
        source_fingerprint=PROJECTION.source_fingerprint,
        now=NOW,
    )


def test_first_attempt_posts_to_parent_chat_after_marking_attempted() -> None:
    store = FakeStore(receipt())
    gateway = FakeGateway()

    publish(service(store, gateway))

    assert store.attempted == 1
    assert store.sent == 1
    assert gateway.find_calls == []
    assert gateway.create_calls == [
        {
            "channel_id": SNAPSHOT.channel_id,
            "content": EXPECTED_CONTENT,
            "nonce": gateway.create_calls[0]["nonce"],
        }
    ]
    assert len(str(gateway.create_calls[0]["nonce"])) <= 25
    assert SNAPSHOT.thread_id != gateway.create_calls[0]["channel_id"]


def test_retry_recovers_existing_message_without_duplicate_post() -> None:
    store = FakeStore(receipt(attempted=True))
    gateway = FakeGateway(existing=True)

    publish(service(store, gateway))

    assert store.attempted == 0
    assert store.sent == 1
    assert gateway.create_calls == []
    assert gateway.find_calls[0]["channel_id"] == SNAPSHOT.channel_id
    assert gateway.find_calls[0]["after_message_id"] == SNAPSHOT.starter_message_id
    assert gateway.find_calls[0]["operation_marker"] == PROJECTION.record_id


def test_missing_receipt_does_not_post_historical_projection() -> None:
    store = FakeStore(None)
    gateway = FakeGateway()

    publish(service(store, gateway))

    assert store.attempted == 0
    assert store.sent == 0
    assert gateway.find_calls == []
    assert gateway.create_calls == []


def test_failed_discord_post_keeps_receipt_pending_for_stream_retry() -> None:
    store = FakeStore(receipt())
    gateway = FakeGateway(fail_create=True)

    with pytest.raises(RuntimeError, match="Discord unavailable"):
        publish(service(store, gateway))

    assert store.attempted == 1
    assert store.sent == 0
    assert store.receipt is not None
    assert store.receipt.state is RecordLinkNotificationState.PENDING
