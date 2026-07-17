"""Convert validated native persistence values to DynamoDB AttributeValues."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.type_defs import AttributeValueTypeDef

from shittim_chest.adapters.dynamodb.serializer import (
    DynamoItem,
    DynamoValue,
    PersistenceFormatError,
)

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()


def marshal_item(item: Mapping[str, DynamoValue]) -> dict[str, AttributeValueTypeDef]:
    """Marshal one native-value item for the low-level transactional API."""

    return {name: _SERIALIZER.serialize(value) for name, value in item.items()}


def unmarshal_item(item: Mapping[str, AttributeValueTypeDef]) -> DynamoItem:
    """Unmarshal one SDK item and reject values outside the persistence schema."""

    return {name: _normalize(_DESERIALIZER.deserialize(value)) for name, value in item.items()}


def _normalize(value: object) -> DynamoValue:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        raise PersistenceFormatError("DynamoDB decimal values are not supported")
    if isinstance(value, list):
        return [_normalize(entry) for entry in value]
    if isinstance(value, dict):
        normalized: dict[str, DynamoValue] = {}
        for name, entry in value.items():
            if not isinstance(name, str):
                raise PersistenceFormatError("DynamoDB map keys must be strings")
            normalized[name] = _normalize(entry)
        return normalized
    raise PersistenceFormatError(f"unsupported DynamoDB value type: {type(value).__name__}")
