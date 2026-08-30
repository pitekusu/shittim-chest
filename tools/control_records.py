#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate, initialize, and guard deployment-owned DynamoDB control records."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shittim_chest.adapters.aws import create_control_records_dynamodb_client
from shittim_chest.adapters.dynamodb.control_records import (
    ControlRecordInitializationError,
    DynamoDbControlRecordInitializer,
)
from shittim_chest.adapters.dynamodb.deployment_guard import (
    DeploymentGuardRejected,
    DeploymentGuardUnavailable,
    DynamoDbDeploymentGuard,
)
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    PREVIOUS_SCHEMA_VERSION,
)
from shittim_chest.application.deployment_guard import (
    DEPLOYMENT_GUARD_AUDIT_SCHEMA_VERSION,
    BreakGlassReason,
    DeploymentGuardAssessment,
    DeploymentGuardCode,
    DeploymentGuardContext,
    DeploymentMode,
)

INITIALIZE_ACKNOWLEDGEMENT = "initialize-control-records"
ACQUIRE_ACKNOWLEDGEMENT = "acquire-deployment-lock"
RELEASE_ACKNOWLEDGEMENT = "release-deployment-lock"


def main(argv: Sequence[str] | None = None) -> int:
    """Run one explicitly selected control-record operation."""

    args = _parser().parse_args(argv)
    if args.command == "validate":
        return _validate(args)
    if args.command == "validate-compatible":
        return _validate_compatible(args)
    if args.command == "initialize":
        return _initialize(args)
    if args.command == "guard":
        return _guard(args)
    if args.command == "acquire":
        return _acquire(args)
    if args.command == "release":
        return _release(args)
    if args.command == "release-decision":
        return _release_decision(args)
    raise AssertionError("unreachable control-record command")


def _validate(args: argparse.Namespace) -> int:
    client = create_control_records_dynamodb_client(region_name=args.region)
    try:
        result = DynamoDbControlRecordInitializer(
            client=client,
            table_name=args.table_name,
        ).validate()
    except ControlRecordInitializationError:
        _print_result({"ok": False, "code": "control_records_invalid"})
        return 3
    _print_result(
        {
            "ok": True,
            "status": result.status.value,
            "manifest_version": result.manifest_version,
            "manifest_hash": result.manifest_hash,
        }
    )
    return 0


def _validate_compatible(args: argparse.Namespace) -> int:
    client = create_control_records_dynamodb_client(region_name=args.region)
    try:
        result = DynamoDbControlRecordInitializer(
            client=client,
            table_name=args.table_name,
        ).validate_compatible()
    except ControlRecordInitializationError:
        _print_result({"ok": False, "code": "control_records_invalid"})
        return 3
    _print_result(
        {
            "ok": True,
            "status": result.status.value,
            "manifest_version": result.manifest_version,
            "manifest_hash": result.manifest_hash,
        }
    )
    return 0


def _initialize(args: argparse.Namespace) -> int:
    if args.acknowledge_write != INITIALIZE_ACKNOWLEDGEMENT:
        _print_result({"ok": False, "code": "write_not_acknowledged"})
        return 2
    client = create_control_records_dynamodb_client(region_name=args.region)
    try:
        result = DynamoDbControlRecordInitializer(
            client=client,
            table_name=args.table_name,
        ).initialize()
    except ControlRecordInitializationError:
        _print_result({"ok": False, "code": "control_record_initialization_failed"})
        return 3
    _print_result(
        {
            "ok": True,
            "status": result.status.value,
            "manifest_version": result.manifest_version,
            "manifest_hash": result.manifest_hash,
        }
    )
    return 0


def _guard(args: argparse.Namespace) -> int:
    evaluated_at = datetime.now(UTC)
    try:
        context = _context(args)
    except ValueError:
        audit = _unavailable_audit(
            context=None,
            break_glass=args.break_glass,
            evaluated_at=evaluated_at,
        )
        _write_audit(args.audit_output, audit)
        _print_result({"allowed": False, "code": DeploymentGuardCode.SNAPSHOT_UNAVAILABLE.value})
        return 3
    client = create_control_records_dynamodb_client(region_name=args.region)
    try:
        assessment = DynamoDbDeploymentGuard(
            client=client,
            table_name=args.table_name,
        ).guard(context=context, evaluated_at=evaluated_at)
    except DeploymentGuardUnavailable:
        audit = _unavailable_audit(
            context=context,
            break_glass=args.break_glass,
            evaluated_at=evaluated_at,
        )
        _write_audit(args.audit_output, audit)
        _print_result({"allowed": False, "code": DeploymentGuardCode.SNAPSHOT_UNAVAILABLE.value})
        return 3
    _write_audit(args.audit_output, _assessment_audit(assessment))
    _print_result({"allowed": assessment.allowed, "code": assessment.code.value})
    return 0 if assessment.allowed else 2


def _acquire(args: argparse.Namespace) -> int:
    if args.acknowledge_write != ACQUIRE_ACKNOWLEDGEMENT:
        _print_result({"ok": False, "code": "write_not_acknowledged"})
        return 2
    acquired_at = datetime.now(UTC)
    context: DeploymentGuardContext | None = None
    try:
        context = _context(args)
        acquisition = DynamoDbDeploymentGuard(
            client=create_control_records_dynamodb_client(region_name=args.region),
            table_name=args.table_name,
        ).acquire(
            context=context,
            guard_id=args.guard_id,
            acquired_at=acquired_at,
            expires_at=acquired_at + timedelta(seconds=args.lock_seconds),
        )
    except DeploymentGuardRejected as error:
        _write_audit(args.audit_output, _assessment_audit(error.assessment))
        _print_result({"ok": False, "code": error.assessment.code.value})
        return 2
    except DeploymentGuardUnavailable, ValueError:
        _write_audit(
            args.audit_output,
            _unavailable_audit(
                context=context,
                break_glass=args.break_glass,
                evaluated_at=acquired_at,
            ),
        )
        _print_result({"ok": False, "code": DeploymentGuardCode.SNAPSHOT_UNAVAILABLE.value})
        return 3
    audit = _assessment_audit(acquisition.assessment)
    audit["guard_id"] = acquisition.lock.guard_id
    audit["lock_fencing_token"] = acquisition.lock.fencing_token
    audit["lock_expires_at"] = _timestamp(acquisition.lock.expires_at or acquired_at)
    audit["control_schema_before"] = acquisition.control_schema_before
    audit["control_schema_after"] = acquisition.control_schema_after
    audit["control_schema_migrated"] = (
        acquisition.control_schema_before != acquisition.control_schema_after
    )
    _write_audit(args.audit_output, audit)
    _print_result(
        {
            "ok": True,
            "code": acquisition.assessment.code.value,
            "guard_id": acquisition.lock.guard_id,
            "lock_fencing_token": acquisition.lock.fencing_token,
        }
    )
    return 0


def _release(args: argparse.Namespace) -> int:
    if args.acknowledge_write != RELEASE_ACKNOWLEDGEMENT:
        _print_result({"ok": False, "code": "write_not_acknowledged"})
        return 2
    released_at = datetime.now(UTC)
    try:
        DynamoDbDeploymentGuard(
            client=create_control_records_dynamodb_client(region_name=args.region),
            table_name=args.table_name,
        ).release(
            guard_id=args.guard_id,
            expected_fencing_token=args.fencing_token,
            actor=args.actor,
            released_at=released_at,
            rollback_control_schema=args.rollback_control_schema,
        )
    except DeploymentGuardUnavailable, ValueError:
        _print_result({"ok": False, "code": "deployment_lock_release_failed"})
        return 3
    _print_result({"ok": True, "code": "deployment_lock_released"})
    return 0


def _release_decision(args: argparse.Namespace) -> int:
    """Choose keep-v8, rollback-v7, or fail-closed from content-free evidence."""

    migrated = args.control_schema_migrated == "true"
    candidate_active = args.runtime_candidate_active == "true"
    evidence_valid = (
        args.control_schema_after == CURRENT_SCHEMA_VERSION
        and migrated == (args.control_schema_before == PREVIOUS_SCHEMA_VERSION)
        and args.control_schema_before in {PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}
    )
    rollback = False
    decision_safe = evidence_valid
    if not migrated:
        decision_safe = decision_safe and (
            args.control_schema_before == CURRENT_SCHEMA_VERSION
            and args.runtime_stack_status == "not-checked"
            and not candidate_active
        )
    elif candidate_active:
        decision_safe = decision_safe and args.runtime_stack_status in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        }
    else:
        rollback = args.runtime_stack_status in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
            "UPDATE_ROLLBACK_COMPLETE",
        }
        decision_safe = decision_safe and rollback
    audit = {
        "schema_version": 1,
        "control_schema_before": args.control_schema_before,
        "control_schema_after": args.control_schema_after,
        "control_schema_migrated": migrated,
        "runtime_stack_status": args.runtime_stack_status,
        "runtime_candidate_active": candidate_active,
        "rollback_requested": rollback,
        "release_decision_safe": decision_safe,
        "lock_released": False,
    }
    _write_audit(args.audit_output, audit)
    _print_result(
        {
            "ok": decision_safe,
            "code": "release_safe" if decision_safe else "release_ambiguous",
            "rollback_control_schema": rollback,
        }
    )
    return 0 if decision_safe else 3


def _context(args: argparse.Namespace) -> DeploymentGuardContext:
    reason = None if args.reason is None else BreakGlassReason(args.reason)
    mode = DeploymentMode.BREAK_GLASS if args.break_glass else DeploymentMode.NORMAL
    return DeploymentGuardContext(
        commit_sha=args.commit_sha,
        actor=args.actor,
        run_id=args.run_id,
        environment=args.environment,
        mode=mode,
        reason=reason,
    )


def _assessment_audit(assessment: DeploymentGuardAssessment) -> dict[str, object]:
    audit: dict[str, object] = {
        "schema_version": DEPLOYMENT_GUARD_AUDIT_SCHEMA_VERSION,
        "allowed": assessment.allowed,
        "code": assessment.code.value,
        "commit_sha": assessment.context.commit_sha,
        "actor": assessment.context.actor,
        "run_id": assessment.context.run_id,
        "environment": assessment.context.environment,
        "mode": assessment.context.mode.value,
        "evaluated_at": _timestamp(assessment.evaluated_at),
        "runtime_status": assessment.runtime_status.value,
        "runtime_generation": assessment.runtime_generation,
        "runtime_version": assessment.runtime_version,
        "activity_clear": assessment.activity_clear,
        "deployment_lock_state": assessment.deployment_lock_state.value,
        "deployment_lock_fencing_token": assessment.deployment_lock_fencing_token,
    }
    if assessment.context.reason is not None:
        audit["reason"] = assessment.context.reason.value
    return audit


def _unavailable_audit(
    *,
    context: DeploymentGuardContext | None,
    break_glass: bool,
    evaluated_at: datetime,
) -> dict[str, object]:
    audit: dict[str, object] = {
        "schema_version": DEPLOYMENT_GUARD_AUDIT_SCHEMA_VERSION,
        "allowed": False,
        "code": DeploymentGuardCode.SNAPSHOT_UNAVAILABLE.value,
        "mode": "break-glass" if break_glass else "normal",
        "evaluated_at": _timestamp(evaluated_at),
    }
    if context is not None:
        audit.update(
            {
                "commit_sha": context.commit_sha,
                "actor": context.actor,
                "run_id": context.run_id,
                "environment": context.environment,
            }
        )
        if context.reason is not None:
            audit["reason"] = context.reason.value
    return audit


def _write_audit(path: Path, audit: dict[str, object]) -> None:
    """Write an owner-only JSON artifact without following an existing symlink."""

    destination = path.expanduser()
    if destination.is_symlink():
        raise ValueError("audit output must not be a symlink")
    parent = destination.parent
    if not parent.is_dir():
        raise ValueError("audit output parent must be an existing directory")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        try:
            os.fchmod(temporary.fileno(), 0o600)
            json.dump(audit, temporary, ensure_ascii=True, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    os.replace(temporary_path, destination)


def _print_result(result: dict[str, object]) -> None:
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True))


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _add_table_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--region", required=True)


def _add_guard_context(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--environment", choices=("production",), required=True)
    parser.add_argument("--break-glass", action="store_true")
    parser.add_argument("--reason", choices=tuple(reason.value for reason in BreakGlassReason))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="read and validate without writing")
    _add_table_options(validate)
    compatible = commands.add_parser(
        "validate-compatible",
        help="validate a current or immediately previous uniform manifest",
    )
    _add_table_options(compatible)
    initialize = commands.add_parser("initialize", help="perform an acknowledged first install")
    _add_table_options(initialize)
    initialize.add_argument("--acknowledge-write", required=True)
    guard = commands.add_parser("guard", help="read and evaluate deployment admission")
    _add_table_options(guard)
    _add_guard_context(guard)
    guard.add_argument("--audit-output", type=Path, required=True)
    acquire = commands.add_parser("acquire", help="atomically acquire the deployment lock")
    _add_table_options(acquire)
    _add_guard_context(acquire)
    acquire.add_argument("--guard-id", required=True)
    acquire.add_argument("--lock-seconds", type=int, choices=range(60, 3601), default=900)
    acquire.add_argument("--acknowledge-write", required=True)
    acquire.add_argument("--audit-output", type=Path, required=True)
    release = commands.add_parser("release", help="release the exact deployment fence")
    _add_table_options(release)
    release.add_argument("--guard-id", required=True)
    release.add_argument("--fencing-token", type=int, required=True)
    release.add_argument("--actor", required=True)
    release.add_argument("--rollback-control-schema", action="store_true")
    release.add_argument("--acknowledge-write", required=True)
    decision = commands.add_parser(
        "release-decision",
        help="derive a fail-closed control-schema release decision",
    )
    decision.add_argument(
        "--control-schema-before",
        type=int,
        choices=(PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION),
        required=True,
    )
    decision.add_argument(
        "--control-schema-after",
        type=int,
        choices=(PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION),
        required=True,
    )
    decision.add_argument("--control-schema-migrated", choices=("true", "false"), required=True)
    decision.add_argument("--runtime-stack-status", required=True)
    decision.add_argument("--runtime-candidate-active", choices=("true", "false"), required=True)
    decision.add_argument("--audit-output", type=Path, required=True)
    return parser


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
