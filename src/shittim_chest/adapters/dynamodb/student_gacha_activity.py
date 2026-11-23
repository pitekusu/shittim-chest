"""Optional activity fence shared by runtime stopping and deployment admission."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shittim_chest.adapters.dynamodb.codec import marshal_item

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

    from shittim_chest.adapters.dynamodb.serializer import DynamoItem

STUDENT_GACHA_ACTIVITY_KEY = {"PK": "CONTROL#GACHA", "SK": "ACTIVITY"}
STUDENT_GACHA_ACTIVITY_TYPE = "student_gacha_activity"


def student_gacha_activity_count(item: DynamoItem | None) -> int:
    if item is None:
        return 0
    count = item.get("count")
    if (
        item.get("record_type") != STUDENT_GACHA_ACTIVITY_TYPE
        or type(count) is not int
        or count < 0
    ):
        raise ValueError("student gacha activity is invalid")
    return count


def student_gacha_idle_check(table_name: str) -> TransactWriteItemTypeDef:
    return {
        "ConditionCheck": {
            "TableName": table_name,
            "Key": marshal_item(STUDENT_GACHA_ACTIVITY_KEY),
            "ConditionExpression": (
                "attribute_not_exists(PK) OR (record_type=:type AND #count=:zero)"
            ),
            "ExpressionAttributeNames": {"#count": "count"},
            "ExpressionAttributeValues": marshal_item(
                {":type": STUDENT_GACHA_ACTIVITY_TYPE, ":zero": 0}
            ),
        }
    }
