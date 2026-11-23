"""The parked-offer list traverses sparse pages without exposing scan keys."""

from __future__ import annotations

import json

import boto3
import pytest
from botocore.stub import Stubber
from tools import recover_student_gacha

from shittim_chest.adapters.dynamodb.codec import marshal_item

_OFFER_ID = "a" * 32
_PARKED_AT = "2026-11-14T02:00:00.000000Z"


def test_list_parked_traverses_an_empty_page_and_hides_scan_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106 - Stubber disables all network I/O.
    )
    monkeypatch.setattr(
        recover_student_gacha, "create_control_records_dynamodb_client", lambda **_: client
    )
    expected = {
        "TableName": "test-table",
        "ConsistentRead": True,
        "FilterExpression": "begins_with(PK, :prefix) AND SK=:meta AND #state=:parked",
        "ProjectionExpression": "PK, parked_at",
        "ExpressionAttributeNames": {"#state": "state"},
        "ExpressionAttributeValues": marshal_item(
            {":prefix": "GACHA#OFFER#", ":meta": "META", ":parked": "delivery_unconfirmed"}
        ),
        "Limit": 250,
    }
    private_key = marshal_item({"PK": "DEBATE#private", "SK": "META"})
    with Stubber(client) as stub:
        stub.add_response("scan", {"Items": [], "LastEvaluatedKey": private_key}, expected)
        stub.add_response(
            "scan",
            {
                "Items": [
                    marshal_item({"PK": f"GACHA#OFFER#{_OFFER_ID}", "parked_at": _PARKED_AT})
                ],
                "LastEvaluatedKey": private_key,
            },
            {**expected, "ExclusiveStartKey": private_key},
        )
        assert (
            recover_student_gacha.main(
                ["--table-name", "test-table", "--list-parked", "--limit", "1"]
            )
            == 0
        )
        stub.assert_no_pending_responses()
    assert json.loads(capsys.readouterr().out) == {
        "parked_offers": [{"offer_id": _OFFER_ID, "parked_at": _PARKED_AT}],
    }


def test_list_parked_cannot_apply() -> None:
    with pytest.raises(SystemExit) as error:
        recover_student_gacha.main(["--table-name", "test-table", "--list-parked", "--apply"])
    assert error.value.code == 2
