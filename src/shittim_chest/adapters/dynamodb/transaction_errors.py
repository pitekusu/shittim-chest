"""Content-free classification for DynamoDB transaction cancellations."""

from __future__ import annotations

from collections.abc import Mapping

from botocore.exceptions import ClientError

from shittim_chest.application.ports import (
    RepositoryCancellationCode,
    RepositoryTransactionAction,
    RepositoryTransactionConflict,
    RepositoryTransactionStage,
)

_CONDITION_ONLY_CODES = frozenset({"None", "ConditionalCheckFailed"})
_CANCELLATION_CODES = {
    code.value: code
    for code in RepositoryCancellationCode
    if code is not RepositoryCancellationCode.UNKNOWN
}


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


def classified_transaction_conflict(
    error: ClientError,
    *,
    stage: RepositoryTransactionStage,
    action_kinds: tuple[RepositoryTransactionAction, ...],
) -> RepositoryTransactionConflict:
    """Map ordered cancellation reasons to allowlisted action/code pairs."""

    raw_reasons = error.response.get("CancellationReasons")
    if not isinstance(raw_reasons, list) or len(raw_reasons) != len(action_kinds):
        return _unknown_transaction_conflict(stage)
    failures: list[tuple[RepositoryTransactionAction, RepositoryCancellationCode]] = []
    reasons_complete = True
    for action, raw_reason in zip(action_kinds, raw_reasons, strict=True):
        if not isinstance(raw_reason, Mapping):
            reasons_complete = False
            continue
        raw_code = raw_reason.get("Code")
        if raw_code == "None":
            continue
        if not isinstance(raw_code, str):
            reasons_complete = False
            continue
        code = _CANCELLATION_CODES.get(raw_code, RepositoryCancellationCode.UNKNOWN)
        if code is RepositoryCancellationCode.UNKNOWN:
            reasons_complete = False
        failures.append((action, code))
    if not failures:
        return _unknown_transaction_conflict(stage)
    return RepositoryTransactionConflict(
        stage=stage,
        failures=tuple(failures),
        reasons_complete=reasons_complete,
    )


def _unknown_transaction_conflict(
    stage: RepositoryTransactionStage,
) -> RepositoryTransactionConflict:
    return RepositoryTransactionConflict(
        stage=stage,
        failures=((RepositoryTransactionAction.UNKNOWN, RepositoryCancellationCode.UNKNOWN),),
        reasons_complete=False,
    )


__all__ = ("classified_transaction_conflict", "is_condition_only_cancellation")
