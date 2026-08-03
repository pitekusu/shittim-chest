"""End-to-end signed request contract through the thin Lambda boundary."""

import json
from dataclasses import fields
from datetime import UTC, datetime
from typing import cast

from nacl.signing import SigningKey

from shittim_chest.adapters.discord_http import (
    DiscordHttpBoundary,
    DiscordRequestVerifier,
)
from shittim_chest.application import DiscordHttpOperation
from shittim_chest.application.ingress import (
    DiscordIngressApplication,
    IngressAcceptance,
    IngressOutcome,
)
from shittim_chest.application.ports import Clock
from shittim_chest.lambda_handlers.discord_ingress import DiscordIngressLambda

NOW = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FakeApplication:
    def __init__(self) -> None:
        self.operations: list[DiscordHttpOperation] = []

    async def accept(self, operation: DiscordHttpOperation) -> IngressAcceptance:
        self.operations.append(operation)
        return IngressAcceptance(IngressOutcome.STARTING, created=True)


def test_signed_command_reaches_token_free_application_and_returns_type_four() -> None:
    signing_key = SigningKey.generate()
    application = FakeApplication()
    handler = DiscordIngressLambda(
        boundary=DiscordHttpBoundary(DiscordRequestVerifier(signing_key.verify_key.encode().hex())),
        application=lambda: cast(DiscordIngressApplication, application),
        clock=cast(Clock, FixedClock()),
    )
    event = _signed_event(signing_key)

    response = handler.handle(event)

    assert response["statusCode"] == 200
    body = json.loads(str(response["body"]))
    assert body["type"] == 4
    assert body["data"]["flags"] == 64
    assert body["data"]["allowed_mentions"] == {"parse": []}
    assert body["data"]["content"] == (
        "✅ 議論依頼を受け付けました。処理開始を準備しています。"
        "\n推定待ち時間: 通常は約1分以内。連続実行時はDiscordの制限により"
        "約5分かかる場合があります。"
        "\nチャンネルへ進行状況を表示します。"
    )
    assert len(application.operations) == 1
    operation = application.operations[0]
    assert operation.question == "question"
    assert "token" not in {item.name for item in fields(operation)}


def test_invalid_signature_returns_401_without_constructing_application() -> None:
    signing_key = SigningKey.generate()
    constructions = 0

    def load() -> DiscordIngressApplication:
        nonlocal constructions
        constructions += 1
        raise AssertionError("unauthenticated requests must not reach application adapters")

    handler = DiscordIngressLambda(
        boundary=DiscordHttpBoundary(DiscordRequestVerifier(signing_key.verify_key.encode().hex())),
        application=load,
        clock=cast(Clock, FixedClock()),
    )
    event = _signed_event(signing_key)
    event["headers"] = {
        "x-signature-ed25519": "00" * 64,
        "x-signature-timestamp": str(int(NOW.timestamp())),
    }

    response = handler.handle(event)

    assert response["statusCode"] == 401
    assert constructions == 0


def _signed_event(signing_key: SigningKey) -> dict[str, object]:
    payload = {
        "version": 1,
        "type": 2,
        "id": "301",
        "application_id": "201",
        "guild_id": "101",
        "channel_id": "102",
        "channel": {"id": "102", "type": 0},
        "member": {
            "user": {"id": "105", "username": "requester", "global_name": None},
            "nick": None,
            "permissions": "0",
        },
        "data": {
            "type": 1,
            "name": "shittim",
            "options": [{"name": "question", "type": 3, "value": "question"}],
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    timestamp = str(int(NOW.timestamp()))
    signature = signing_key.sign(timestamp.encode("ascii") + body.encode("utf-8")).signature
    return {
        "version": "2.0",
        "headers": {
            "x-signature-ed25519": signature.hex(),
            "x-signature-timestamp": timestamp,
        },
        "requestContext": {"http": {"method": "POST"}},
        "body": body,
        "isBase64Encoded": False,
    }
