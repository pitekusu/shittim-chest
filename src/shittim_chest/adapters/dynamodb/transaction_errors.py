"""Content-free classification for DynamoDB transaction cancellations."""

from __future__ import annotations

from collections.abc import Mapping

from botocore.exceptions import ClientError

_CONDITION_ONLY_CODES = frozenset({"None", "ConditionalCheckFailed"})


def is_condition_only_cancellation(error: ClientError) -> bool:
    """Return true only when every reported cancellation is an expected condition miss."""

    raw_reasons = error.response.get("CancellationReasons")
    if not isinstance(raw_reasons, list) or not raw_reasons:
        return False
    codes: list[str] = []
    for raw_reason in raw_reasons:
        if not isinstance(raw_reason, Mapping):
            return False
        code = raw_reason.get("Code")
        if not isinstance(code, str):
            return False
        codes.append(code)
    return "ConditionalCheckFailed" in codes and all(
        code in _CONDITION_ONLY_CODES for code in codes
    )


__all__ = ("is_condition_only_cancellation",)
