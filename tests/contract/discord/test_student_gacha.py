"""Signed Arona interaction authorization and Discord delivery recovery."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import discord
import pytest
from nacl.signing import SigningKey

from shittim_chest.adapters.discord import student_gacha as delivery
from shittim_chest.adapters.discord_http import (
    STUDENT_GACHA_BUTTON_PREFIX,
    STUDENT_GACHA_MODAL_PREFIX,
    STUDENT_GACHA_ROUTE,
    DiscordHttpBoundary,
    DiscordRequestVerifier,
)
from shittim_chest.application.discord import (
    DISCORD_BOT_SLOTS,
    DiscordBotSlot,
    DiscordIdentityConfig,
    DiscordRuntimeConfig,
)
from shittim_chest.application.ingress import DiscordIngressApplication
from shittim_chest.application.student_gacha_discord import (
    GachaDeliveryFailure,
    StudentGachaInteractions,
)
from shittim_chest.application.student_gacha_state import GachaOffer
from shittim_chest.lambda_handlers.discord_ingress import DiscordIngressLambda

NOW = datetime(2026, 11, 13, tzinfo=UTC)
OFFER_ID = "a" * 32
MODERATOR = SigningKey(b"a" * 32)
ARONA = SigningKey(b"b" * 32)


@dataclass
class _Clock:
    def now(self) -> datetime:
        return NOW


def _config() -> DiscordRuntimeConfig:
    return DiscordRuntimeConfig(
        guild_id="101",
        allowed_channel_ids=frozenset({"102"}),
        farewell_channel_id="102",
        identities=tuple(
            DiscordIdentityConfig(slot=slot, application_id=str(number))
            for number, slot in enumerate(DISCORD_BOT_SLOTS, start=201)
        ),
        schema_version="2",
    )


class _Store:
    def __init__(self) -> None:
        self.warning_available = True
        self.owner_id = "104"
        self.warning_count = 0
        self.submissions: list[dict[str, object]] = []
        self.reads = 0

    async def get_offer(self, offer_id: str) -> GachaOffer | None:
        self.reads += 1
        assert offer_id == OFFER_ID
        return cast(
            GachaOffer,
            SimpleNamespace(
                live=True,
                guild_id="101",
                channel_id="102",
                message_id="103",
                requester_id=self.owner_id,
            ),
        )

    async def claim_warning(
        self, *, offer_id: str, guild_id: str, channel_id: str, message_id: str, at: datetime
    ) -> bool:
        assert (offer_id, guild_id, channel_id, message_id, at) == (
            OFFER_ID,
            "101",
            "102",
            "103",
            NOW,
        )
        self.warning_count += 1
        available = self.warning_available
        self.warning_available = False
        return available

    async def submit(self, **values: object) -> bool:
        self.submissions.append(values)
        return values["requester_id"] == "104" and values["message_id"] == "103"


def _lambda(store: _Store) -> DiscordIngressLambda:
    def old_application() -> DiscordIngressApplication:
        raise AssertionError("gacha must use the separate application")

    return DiscordIngressLambda(
        boundary=DiscordHttpBoundary(
            DiscordRequestVerifier(bytes(MODERATOR.verify_key).hex()),
            student_gacha_verifier=DiscordRequestVerifier(bytes(ARONA.verify_key).hex()),
        ),
        clock=_Clock(),
        application=old_application,
        student_gacha=lambda: StudentGachaInteractions(store=store, config=_config()),
    )


def _event(
    *,
    signing_key: SigningKey = ARONA,
    route: str = STUDENT_GACHA_ROUTE,
    requester: str = "104",
    message_id: str = "103",
    application_id: str = "202",
    author_id: str = "202",
    teacher_name: str | None = None,
    channel_type: int = 0,
) -> dict[str, object]:
    data: dict[str, object] = {
        "component_type": 2,
        "custom_id": STUDENT_GACHA_BUTTON_PREFIX + OFFER_ID + ":" + "c" * 22,
    }
    if teacher_name is not None:
        data = {
            "custom_id": STUDENT_GACHA_MODAL_PREFIX + OFFER_ID,
            "components": [
                {
                    "type": 18,
                    "component": {"type": 4, "custom_id": "teacher_name", "value": teacher_name},
                },
            ],
        }
    body = json.dumps(
        {
            "type": 3 if teacher_name is None else 5,
            "version": 1,
            "id": "111",
            "application_id": application_id,
            "guild_id": "101",
            "channel_id": "102",
            "channel": {"id": "102", "type": channel_type},
            "member": {"user": {"id": requester}, "permissions": "8192"},
            "message": {"id": message_id, "channel_id": "102", "author": {"id": author_id}},
            "data": data,
        },
    )
    timestamp = str(int(NOW.timestamp()))
    signature = signing_key.sign(timestamp.encode() + body.encode()).signature.hex()
    return {
        "version": "2.0",
        "requestContext": {"http": {"method": "POST"}, "routeKey": route},
        "headers": {"x-signature-ed25519": signature, "x-signature-timestamp": timestamp},
        "body": body,
    }


def _body(response: dict[str, object]) -> dict[str, object]:
    assert response["statusCode"] == 200
    body = response["body"]
    assert isinstance(body, str)
    return cast(dict[str, object], json.loads(body))


def test_owner_opens_current_label_modal_and_submits_normalized_name() -> None:
    store = _Store()
    handler = _lambda(store)
    modal = _body(handler.handle(_event()))
    assert modal["type"] == 9
    data = cast(dict[str, object], modal["data"])
    assert data["custom_id"] == STUDENT_GACHA_MODAL_PREFIX + OFFER_ID
    label = cast(list[dict[str, object]], data["components"])[0]
    assert label["type"] == 18 and label["label"] == "先生の名前"
    assert label["component"] == {
        "type": 4,
        "custom_id": "teacher_name",
        "style": 1,
        "required": True,
        "min_length": 1,
        "max_length": 24,
    }
    assert store.submissions == []

    response = _body(handler.handle(_event(teacher_name=" 先生か\u3099 ")))
    assert response["type"] == 4
    assert cast(dict[str, object], response["data"])["flags"] == 64
    assert len(store.submissions) == 1
    assert store.submissions[0]["teacher_name"] == "先生が"
    assert store.submissions[0]["message_id"] == "103"


def test_wrong_owner_warning_is_public_only_once_and_never_opens_modal() -> None:
    store = _Store()
    handler = _lambda(store)
    first = _body(handler.handle(_event(requester="105")))
    assert first["type"] == 4
    warning = cast(dict[str, object], first["data"])
    assert "flags" not in warning
    assert warning["allowed_mentions"] == {"parse": [], "users": ["104", "105"]}
    assert warning["content"] == (
        "今回の生徒募集は<@104>先生だけができるんです\uff01<@105>先生は押しちゃダメですよっ\uff01"
    )

    repeated = _body(handler.handle(_event(requester="105")))
    assert cast(dict[str, object], repeated["data"])["flags"] == 64
    assert store.submissions == []


def test_invalid_stored_owner_cannot_be_interpolated_into_a_warning() -> None:
    store = _Store()
    store.owner_id = "104> @everyone"
    response = _body(_lambda(store).handle(_event(requester="105")))
    data = cast(dict[str, object], response["data"])
    assert data["flags"] == 64
    assert data["allowed_mentions"] == {"parse": []}
    assert store.warning_count == 0


def test_stale_or_wrong_origin_cannot_trigger_a_public_warning() -> None:
    store = _Store()
    handler = _lambda(store)
    for event in (
        _event(requester="105", message_id="150"),
        _event(requester="105", application_id="201"),
        _event(requester="105", author_id="201"),
        _event(requester="105", channel_type=11),
    ):
        response = _body(handler.handle(event))
        assert cast(dict[str, object], response["data"])["flags"] == 64
    assert store.reads == 1
    assert store.warning_count == 0


def test_route_uses_its_own_signature_key_before_store_access() -> None:
    store = _Store()
    handler = _lambda(store)
    assert handler.handle(_event(signing_key=MODERATOR))["statusCode"] == 401
    assert handler.handle(_event(route="POST /interactions/discord"))["statusCode"] == 401
    assert store.reads == 0


def test_invalid_name_and_stale_modal_never_acknowledge_generation() -> None:
    store = _Store()
    handler = _lambda(store)
    invalid = _body(handler.handle(_event(teacher_name="先生\n別名")))
    assert "文字" in cast(str, cast(dict[str, object], invalid["data"])["content"])
    assert store.submissions == []
    stale = _body(handler.handle(_event(teacher_name="先生", message_id="150")))
    assert "利用できません" in cast(str, cast(dict[str, object], stale["data"])["content"])


@pytest.mark.asyncio
async def test_progress_recovers_nonce_less_message_without_reposting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = delivery.DiscordStudentGacha(
        clients={DiscordBotSlot.PARTICIPANT_A: cast(discord.Client, object())},
        config=_config(),
    )
    channel = SimpleNamespace(id=102, send=AsyncMock())

    async def history(**_kwargs: object) -> AsyncIterator[object]:
        yield SimpleNamespace(
            id=120,
            author=SimpleNamespace(id=202),
            channel=channel,
            nonce=None,
            content="✨ あと4回で生徒ガチャにチャレンジできます!",
            components=[],
            embeds=[],
            reference=discord.MessageReference(message_id=103, channel_id=102),
            attachments=[],
        )

    channel.history = history
    monkeypatch.setattr(gateway, "_channel", AsyncMock(return_value=channel))
    assert (
        await gateway.send_progress(
            guild_id="101",
            channel_id="102",
            publication_id="debate-progress",
            after_message_id="103",
            remaining=4,
        )
        == "120"
    )
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_gallery_notice_links_to_gallery_and_recovers_the_same_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = delivery.DiscordStudentGacha(
        clients={DiscordBotSlot.PARTICIPANT_A: cast(discord.Client, object())},
        config=_config(),
    )
    channel = SimpleNamespace(id=102, guild=SimpleNamespace(id=101), send=AsyncMock())
    content = "過去の募集結果は[ギャラリー](https://shittim.pitekusu.dev/gallery)で見れますよ\uff01"
    notice = SimpleNamespace(
        id=121,
        author=SimpleNamespace(id=202),
        channel=channel,
        nonce=None,
        content=content,
        components=[],
        embeds=[],
        reference=discord.MessageReference(message_id=120, channel_id=102),
        attachments=[],
    )

    async def history(**kwargs: object) -> AsyncIterator[object]:
        assert kwargs["after"] == discord.Object(id=120)
        if channel.send.await_count:
            yield notice

    channel.history = history
    channel.send.return_value = notice
    monkeypatch.setattr(gateway, "_channel", AsyncMock(return_value=channel))

    for _ in range(2):
        assert (
            await gateway.send_gallery_notice(
                guild_id="101", channel_id="102", publication_id=OFFER_ID, result_message_id="120"
            )
            == "121"
        )

    channel.send.assert_awaited_once()
    assert channel.send.await_args.args == (content,)
    options = channel.send.await_args.kwargs
    assert options["reference"].message_id == 120
    assert options["nonce"] == delivery._nonce(f"{OFFER_ID}:gallery")
    assert options["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert options["suppress_embeds"] is True


@pytest.mark.asyncio
async def test_loading_cleanup_checks_sender_and_attachment_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = delivery.DiscordStudentGacha(
        clients={DiscordBotSlot.PARTICIPANT_A: cast(discord.Client, object())},
        config=_config(),
    )
    channel = SimpleNamespace(id=102, fetch_message=AsyncMock())
    message = SimpleNamespace(
        author=SimpleNamespace(id=202),
        channel=channel,
        content="⏳ 生徒を募集しています!",
        attachments=[SimpleNamespace(filename="student.png")],
        delete=AsyncMock(),
    )
    channel.fetch_message.return_value = message
    monkeypatch.setattr(gateway, "_channel", AsyncMock(return_value=channel))
    with pytest.raises(GachaDeliveryFailure, match="student_gacha_discord_delivery_failed"):
        await gateway.delete_loading(guild_id="101", channel_id="102", message_id="150")
    message.delete.assert_not_awaited()

    message.attachments[0].filename = f"show-{delivery._nonce('draw')}.gif"
    await gateway.delete_loading(guild_id="101", channel_id="102", message_id="150")
    message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_reoffer_does_not_recover_an_older_message_with_the_same_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = delivery.DiscordStudentGacha(
        clients={DiscordBotSlot.PARTICIPANT_A: cast(discord.Client, object())},
        config=_config(),
    )
    channel = SimpleNamespace(id=102, send=AsyncMock())

    def offer_message(publication_id: str, message_id: int) -> object:
        nonce = delivery._nonce(publication_id)
        return SimpleNamespace(
            id=message_id,
            author=SimpleNamespace(id=202),
            channel=channel,
            nonce=None,
            content=delivery.STUDENT_GACHA_APPROVAL,
            components=[
                discord.ActionRow(
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2,
                                "style": 1,
                                "label": "１回募集",
                                "custom_id": f"{STUDENT_GACHA_BUTTON_PREFIX}{OFFER_ID}:{nonce}",
                            },
                        ],
                    },
                ),
            ],
            embeds=[],
            attachments=[],
        )

    async def history(**_kwargs: object) -> AsyncIterator[object]:
        yield offer_message("old", 120)

    channel.history = history
    channel.send.return_value = offer_message("new", 121)
    monkeypatch.setattr(gateway, "_channel", AsyncMock(return_value=channel))
    assert (
        await gateway.send_offer(
            guild_id="101",
            channel_id="102",
            publication_id="new",
            after_message_id="103",
            offer_id=OFFER_ID,
        )
        == "121"
    )
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("history_case", ["missing", "matching", "conflict", "truncated"])
async def test_find_loading_only_reconciles_verified_history(
    monkeypatch: pytest.MonkeyPatch, history_case: str
) -> None:
    gateway = delivery.DiscordStudentGacha(
        clients={DiscordBotSlot.PARTICIPANT_A: cast(discord.Client, object())},
        config=_config(),
    )
    channel = SimpleNamespace(id=102, send=AsyncMock())
    filename = f"show-{delivery._nonce('draw:loading')}.gif"

    async def history(**kwargs: object) -> AsyncIterator[object]:
        assert cast(discord.Object, kwargs["after"]).id == 103
        assert kwargs["limit"] == delivery._HISTORY_LIMIT + 1
        if history_case in {"matching", "conflict"}:
            yield SimpleNamespace(
                id=120,
                author=SimpleNamespace(id=202),
                channel=channel,
                nonce=None,
                content="⏳ 生徒を募集しています!",
                components=[],
                attachments=[
                    SimpleNamespace(
                        filename=filename,
                        content_type="image/gif" if history_case == "matching" else "image/png",
                        size=10,
                    ),
                ],
            )
        elif history_case == "truncated":
            for _ in range(delivery._HISTORY_LIMIT + 1):
                yield SimpleNamespace(author=SimpleNamespace(id=555))

    channel.history = history
    resolve = AsyncMock(return_value=channel)
    monkeypatch.setattr(gateway, "_channel", resolve)

    async def find() -> str | None:
        return await gateway.find_loading(
            guild_id="101",
            channel_id="102",
            publication_id="draw",
            after_message_id="103",
            content=b"GIF89a",
        )

    if history_case in {"conflict", "truncated"}:
        with pytest.raises(GachaDeliveryFailure) as caught:
            await find()
        assert caught.value.ambiguous
    else:
        assert await find() == ("120" if history_case == "matching" else None)
    resolve.assert_awaited_once_with("101", "102", send_messages=False)
    channel.send.assert_not_awaited()
