"""Pure authorization and immutable prompt-revision services for Records ADMIN."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from shittim_records.archive import derive_requester_key
from shittim_records.auth import SessionRecord, csrf_hash, session_hash

PROMPT_KEYS = (
    "system",
    "moderator",
    "participant-a",
    "participant-b",
    "participant-c",
)
PromptKey = Literal[
    "system",
    "moderator",
    "participant-a",
    "participant-b",
    "participant-c",
]
PromptAction = Literal["publish", "rollback"]
SYSTEM_PROMPT_CONFIRMATION = "APPLY SYSTEM PROMPT"
MAX_PROMPT_BYTES = 3_500
REVISION_PATTERN = re.compile(r"^r[0-9a-hjkmnp-tv-z]{26}$")
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


class AdminFailure(RuntimeError):
    """Stable content-free failure raised at the ADMIN boundary."""

    def __init__(self, code: str, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class PromptRevisionIncomplete(RuntimeError):
    """An inactive immutable revision was only partially persisted."""

    def __init__(self) -> None:
        super().__init__("prompt revision is incomplete")


@dataclass(frozen=True, slots=True)
class AdminSecurityConfiguration:
    identity_hmac_key: bytes = field(repr=False)
    session_hmac_key: bytes = field(repr=False)
    admin_discord_user_id: str = field(repr=False)
    allowed_origin: str


@dataclass(frozen=True, slots=True)
class PromptValues:
    """Five normalized private prompts whose values never appear in repr output."""

    system: str = field(repr=False)
    moderator: str = field(repr=False)
    participant_a: str = field(repr=False)
    participant_b: str = field(repr=False)
    participant_c: str = field(repr=False)

    @classmethod
    def from_mapping(cls, values: Mapping[Any, object]) -> PromptValues:
        if set(values) != set(PROMPT_KEYS):
            raise AdminFailure("PROMPT_INVALID", 400)
        normalized = {key: normalize_prompt(values[key]) for key in PROMPT_KEYS}
        return cls(
            system=normalized["system"],
            moderator=normalized["moderator"],
            participant_a=normalized["participant-a"],
            participant_b=normalized["participant-b"],
            participant_c=normalized["participant-c"],
        )

    def as_mapping(self) -> dict[PromptKey, str]:
        return {
            "system": self.system,
            "moderator": self.moderator,
            "participant-a": self.participant_a,
            "participant-b": self.participant_b,
            "participant-c": self.participant_c,
        }

    def checksums(self) -> dict[PromptKey, str]:
        return {
            key: hashlib.sha256(value.encode()).hexdigest()
            for key, value in self.as_mapping().items()
        }


@dataclass(frozen=True, slots=True)
class PromptManifest:
    revision: str
    created_at: datetime
    action: PromptAction
    base_revision: str | None
    checksums: Mapping[PromptKey, str]


@dataclass(frozen=True, slots=True)
class PromptRevision:
    manifest: PromptManifest
    prompts: PromptValues = field(repr=False)


@dataclass(frozen=True, slots=True)
class PromptRevisionSummary:
    revision: str
    created_at: datetime
    action: PromptAction
    base_revision: str | None
    source_revision: str | None
    checksum: str


@dataclass(frozen=True, slots=True)
class PromptHistoryPage:
    items: tuple[PromptRevisionSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class PromptOperation:
    idempotency_hash: str
    request_hash: str
    revision: str
    created_at: datetime
    action: PromptAction
    base_revision: str | None
    source_revision: str | None
    complete: bool


@dataclass(frozen=True, slots=True)
class PromptCurrent:
    mode: Literal["legacy", "managed"]
    revision: PromptRevision | None
    prompts: PromptValues = field(repr=False)


class AdminSessionStore(Protocol):
    def get_session(self, *, session_hash: str) -> SessionRecord | None: ...


class PromptRevisionStore(Protocol):
    def load_active_revision_id(self) -> str | None: ...

    def load_revision(self, revision: str) -> PromptRevision: ...

    def create_revision(self, revision: PromptRevision) -> None: ...

    def activate(self, *, revision: str, expected_base_revision: str | None) -> None: ...


class LegacyPromptSource(Protocol):
    def load(self) -> PromptValues: ...


class PromptAuditStore(Protocol):
    def get_operation(self, idempotency_hash: str) -> PromptOperation | None: ...

    def get_pending_operation(self, request_hash: str) -> PromptOperation | None: ...

    def get_pending_for_active_revision(self, revision: str) -> PromptOperation | None: ...

    def get_pending_operation_any(self) -> PromptOperation | None: ...

    def begin_operation(
        self,
        *,
        idempotency_hash: str,
        request_hash: str,
        revision: str,
        created_at: datetime,
        action: PromptAction,
        expected_base_revision: str | None,
        source_revision: str | None,
    ) -> PromptOperation: ...

    def complete_operation(
        self,
        *,
        operation: PromptOperation,
        summary: PromptRevisionSummary,
    ) -> None: ...

    def abort_operation(self, *, operation: PromptOperation) -> None: ...

    def get_summary(self, revision: str) -> PromptRevisionSummary | None: ...

    def list_summaries(self, *, limit: int, cursor: str | None) -> PromptHistoryPage: ...


class AdminAuthorizer:
    """Authenticate a Session then derive the exact administrator key for every request."""

    def __init__(
        self,
        *,
        store: AdminSessionStore,
        configuration: AdminSecurityConfiguration,
    ) -> None:
        self._store = store
        self._configuration = configuration

    @property
    def allowed_origin(self) -> str:
        return self._configuration.allowed_origin

    def authenticate(self, *, raw_session: str | None, now: datetime) -> SessionRecord:
        if not raw_session:
            raise AdminFailure("AUTHENTICATION_REQUIRED", 401)
        stored = self._store.get_session(
            session_hash=session_hash(self._configuration.session_hmac_key, raw_session)
        )
        if stored is None or stored.expires_at <= int(_utc(now).timestamp()):
            raise AdminFailure("AUTHENTICATION_REQUIRED", 401)
        expected = derive_requester_key(
            self._configuration.identity_hmac_key,
            self._configuration.admin_discord_user_id,
        )
        if not hmac.compare_digest(expected, stored.requester_key):
            raise AdminFailure("ADMIN_ACCESS_DENIED", 403)
        return stored

    def authorize_write(
        self,
        *,
        session: SessionRecord,
        raw_csrf: str | None,
        csrf_header: str | None,
        origin: str | None,
        idempotency_key: str | None,
    ) -> str:
        if origin != self._configuration.allowed_origin:
            raise AdminFailure("ORIGIN_INVALID", 403)
        if not raw_csrf or not csrf_header or not hmac.compare_digest(raw_csrf, csrf_header):
            raise AdminFailure("CSRF_INVALID", 403)
        expected_csrf = csrf_hash(self._configuration.session_hmac_key, raw_csrf)
        if not hmac.compare_digest(expected_csrf, session.csrf_hash):
            raise AdminFailure("CSRF_INVALID", 403)
        if idempotency_key is None or IDEMPOTENCY_PATTERN.fullmatch(idempotency_key) is None:
            raise AdminFailure("IDEMPOTENCY_KEY_INVALID", 400)
        return hashlib.sha256(idempotency_key.encode()).hexdigest()


class AdminPromptService:
    """Create and activate immutable prompt revisions without exposing their values."""

    def __init__(
        self,
        *,
        revisions: PromptRevisionStore,
        legacy: LegacyPromptSource,
        audit: PromptAuditStore,
    ) -> None:
        self._revisions = revisions
        self._legacy = legacy
        self._audit = audit

    def get_current(self) -> PromptCurrent:
        current = self._load_current_without_recovery()
        active = None if current.revision is None else current.revision.manifest.revision
        pending = self._audit.get_pending_operation_any()
        if pending is None:
            return current
        if pending.revision == active:
            if current.revision is None:
                raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
            self._audit.complete_operation(
                operation=pending,
                summary=_summary_from_operation(pending, current.revision.manifest),
            )
            return current
        if pending.base_revision != active:
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        try:
            revision = self._revisions.load_revision(pending.revision)
        except PromptRevisionIncomplete:
            self._audit.abort_operation(operation=pending)
            return current
        self._revisions.activate(
            revision=pending.revision,
            expected_base_revision=pending.base_revision,
        )
        self._audit.complete_operation(
            operation=pending,
            summary=_summary_from_operation(pending, revision.manifest),
        )
        return PromptCurrent(mode="managed", revision=revision, prompts=revision.prompts)

    def apply(
        self,
        *,
        base_revision: str | None,
        prompts: Mapping[Any, object],
        system_confirmation: str | None,
        idempotency_hash: str,
        now: datetime,
    ) -> PromptRevisionSummary:
        normalized = PromptValues.from_mapping(prompts)
        return self._save(
            action="publish",
            base_revision=base_revision,
            source_revision=None,
            prompts=normalized,
            idempotency_hash=idempotency_hash,
            now=now,
            system_confirmation=system_confirmation,
        )

    def rollback(
        self,
        *,
        base_revision: str,
        source_revision: str,
        system_confirmation: str | None,
        idempotency_hash: str,
        now: datetime,
    ) -> PromptRevisionSummary:
        _validate_revision(base_revision)
        _validate_revision(source_revision)
        source_summary = self._audit.get_summary(source_revision)
        if source_summary is None:
            raise AdminFailure("PROMPT_REVISION_NOT_FOUND", 404)
        source = self._load_revision_for_read(source_revision)
        _require_summary_matches_manifest(source_summary, source.manifest)
        return self._save(
            action="rollback",
            base_revision=base_revision,
            source_revision=source_revision,
            prompts=source.prompts,
            idempotency_hash=idempotency_hash,
            now=now,
            system_confirmation=system_confirmation,
        )

    def get_revision(self, revision: str) -> tuple[PromptRevisionSummary, PromptRevision]:
        _validate_revision(revision)
        summary = self._audit.get_summary(revision)
        if summary is None:
            raise AdminFailure("PROMPT_REVISION_NOT_FOUND", 404)
        stored = self._load_revision_for_read(revision)
        _require_summary_matches_manifest(summary, stored.manifest)
        return summary, stored

    def list_revisions(self, *, limit: int, cursor: str | None) -> PromptHistoryPage:
        if not 1 <= limit <= 50:
            raise AdminFailure("REQUEST_INVALID", 400)
        if cursor is not None:
            _validate_revision(cursor)
        return self._audit.list_summaries(limit=limit, cursor=cursor)

    def _save(
        self,
        *,
        action: PromptAction,
        base_revision: str | None,
        source_revision: str | None,
        prompts: PromptValues,
        idempotency_hash: str,
        now: datetime,
        system_confirmation: str | None,
    ) -> PromptRevisionSummary:
        now = _utc(now).replace(microsecond=0)
        request_hash = _request_hash(
            action=action,
            base_revision=base_revision,
            source_revision=source_revision,
            prompts=prompts,
        )
        operation = self._audit.get_operation(idempotency_hash)
        if operation is None:
            try:
                operation = self._audit.get_pending_operation(request_hash)
            except AdminFailure as error:
                if error.code != "PROMPT_REVISION_CONFLICT":
                    raise
                # A previous invocation may have switched the SSM pointer but
                # crashed before its audit transaction. Reconcile that exact
                # durable binding before attempting a new request.
                current = self.get_current()
            else:
                current = self._load_current_without_recovery()
        else:
            current = (
                self.get_current() if operation.complete else self._load_current_without_recovery()
            )
        if operation is not None:
            # get_current() may have finalized a pointer-switched operation.
            operation = self._audit.get_operation(operation.idempotency_hash)
            if operation is None:
                raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503)
        if operation is None:
            self._require_current_base(current=current, base_revision=base_revision)
            if current.mode == "managed" and hmac.compare_digest(
                aggregate_checksum(current.prompts.checksums()),
                aggregate_checksum(prompts.checksums()),
            ):
                raise AdminFailure("PROMPT_CONTENT_UNCHANGED", 409)
            self._require_system_confirmation(
                current=current.prompts,
                proposed=prompts,
                confirmation=system_confirmation,
            )
            operation = self._audit.begin_operation(
                idempotency_hash=idempotency_hash,
                request_hash=request_hash,
                revision=new_revision_id(now),
                created_at=now,
                action=action,
                expected_base_revision=base_revision,
                source_revision=source_revision,
            )
        if not hmac.compare_digest(operation.request_hash, request_hash):
            raise AdminFailure("IDEMPOTENCY_CONFLICT", 409)
        if operation.complete:
            existing = self._audit.get_summary(operation.revision)
            if existing is None:
                raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503)
            return existing
        manifest = PromptManifest(
            revision=operation.revision,
            created_at=operation.created_at,
            action=action,
            base_revision=base_revision,
            checksums=prompts.checksums(),
        )
        revision = PromptRevision(manifest=manifest, prompts=prompts)
        self._revisions.create_revision(revision)
        self._revisions.activate(
            revision=operation.revision,
            expected_base_revision=base_revision,
        )
        summary = _summary_from_operation(operation, manifest)
        self._audit.complete_operation(operation=operation, summary=summary)
        return summary

    def _load_current_without_recovery(self) -> PromptCurrent:
        active = self._revisions.load_active_revision_id()
        if active is None:
            return PromptCurrent(mode="legacy", revision=None, prompts=self._legacy.load())
        revision = self._load_revision_for_read(active)
        return PromptCurrent(mode="managed", revision=revision, prompts=revision.prompts)

    def _load_revision_for_read(self, revision: str) -> PromptRevision:
        try:
            return self._revisions.load_revision(revision)
        except PromptRevisionIncomplete:
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None

    @staticmethod
    def _require_current_base(
        *,
        current: PromptCurrent,
        base_revision: str | None,
    ) -> None:
        active = None if current.revision is None else current.revision.manifest.revision
        if base_revision is not None:
            _validate_revision(base_revision)
        if active != base_revision:
            raise AdminFailure("PROMPT_REVISION_CONFLICT", 409)

    @staticmethod
    def _require_system_confirmation(
        *,
        current: PromptValues,
        proposed: PromptValues,
        confirmation: str | None,
    ) -> None:
        changed = not hmac.compare_digest(current.system.encode(), proposed.system.encode())
        if changed and confirmation != SYSTEM_PROMPT_CONFIRMATION:
            raise AdminFailure("SYSTEM_PROMPT_CONFIRMATION_REQUIRED", 400)
        if not changed and confirmation not in (None, ""):
            raise AdminFailure("REQUEST_INVALID", 400)


def normalize_prompt(value: object) -> str:
    if not isinstance(value, str):
        raise AdminFailure("PROMPT_INVALID", 400)
    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if not normalized.strip() or len(normalized.encode()) > MAX_PROMPT_BYTES:
        raise AdminFailure("PROMPT_INVALID", 400)
    return normalized


def new_revision_id(now: datetime) -> str:
    milliseconds = int(_utc(now).timestamp() * 1000)
    if not 0 <= milliseconds < 2**48:
        raise ValueError("revision timestamp is outside the ULID range")
    value = (milliseconds << 80) | int.from_bytes(secrets.token_bytes(10))
    characters = ["0"] * 26
    for index in range(25, -1, -1):
        characters[index] = _CROCKFORD[value & 31]
        value >>= 5
    revision = "r" + "".join(characters)
    _validate_revision(revision)
    return revision


def manifest_json(manifest: PromptManifest) -> str:
    _validate_revision(manifest.revision)
    if manifest.base_revision is not None:
        _validate_revision(manifest.base_revision)
    checksums = dict(manifest.checksums)
    _validate_checksums(checksums)
    payload = {
        "schema_version": "1",
        "revision": manifest.revision,
        "created_at": _format_utc_seconds(manifest.created_at),
        "action": manifest.action,
        "base_revision": manifest.base_revision,
        "checksums": checksums,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def parse_manifest(raw: str) -> PromptManifest:
    try:
        payload = json.loads(raw)
    except TypeError, ValueError, json.JSONDecodeError:
        raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503) from None
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "revision",
        "created_at",
        "action",
        "base_revision",
        "checksums",
    }:
        raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
    revision = payload.get("revision")
    base_revision = payload.get("base_revision")
    action = payload.get("action")
    checksums = payload.get("checksums")
    if (
        payload.get("schema_version") != "1"
        or not isinstance(revision, str)
        or (base_revision is not None and not isinstance(base_revision, str))
        or action not in {"publish", "rollback"}
        or not isinstance(checksums, dict)
    ):
        raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
    try:
        _validate_revision(revision)
        if base_revision is not None:
            _validate_revision(base_revision)
        _validate_checksums(checksums)
        created_at = _parse_utc_seconds(payload.get("created_at"))
    except AdminFailure, TypeError, ValueError:
        raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503) from None
    return PromptManifest(
        revision=revision,
        created_at=created_at,
        action=action,
        base_revision=base_revision,
        checksums=checksums,
    )


def aggregate_checksum(checksums: Mapping[Any, str]) -> str:
    _validate_checksums(checksums)
    payload = json.dumps(dict(checksums), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _request_hash(
    *,
    action: PromptAction,
    base_revision: str | None,
    source_revision: str | None,
    prompts: PromptValues,
) -> str:
    payload = {
        "action": action,
        "base_revision": base_revision,
        "source_revision": source_revision,
        "checksums": prompts.checksums(),
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _summary_from_operation(
    operation: PromptOperation,
    manifest: PromptManifest,
) -> PromptRevisionSummary:
    if (
        operation.revision != manifest.revision
        or operation.action != manifest.action
        or operation.base_revision != manifest.base_revision
        or (operation.action == "publish" and operation.source_revision is not None)
        or (operation.action == "rollback" and operation.source_revision is None)
    ):
        raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
    return PromptRevisionSummary(
        revision=operation.revision,
        created_at=operation.created_at,
        action=operation.action,
        base_revision=operation.base_revision,
        source_revision=operation.source_revision,
        checksum=aggregate_checksum(manifest.checksums),
    )


def _require_summary_matches_manifest(
    summary: PromptRevisionSummary,
    manifest: PromptManifest,
) -> None:
    if (
        summary.revision != manifest.revision
        or summary.created_at != manifest.created_at
        or summary.action != manifest.action
        or summary.base_revision != manifest.base_revision
        or not hmac.compare_digest(summary.checksum, aggregate_checksum(manifest.checksums))
    ):
        raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)


def _validate_revision(value: str) -> None:
    if REVISION_PATTERN.fullmatch(value) is None:
        raise AdminFailure("PROMPT_REVISION_INVALID", 400)


def _validate_checksums(checksums: Mapping[Any, object]) -> None:
    if set(checksums) != set(PROMPT_KEYS) or any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in checksums.values()
    ):
        raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)


def _format_utc_seconds(value: datetime) -> str:
    return _utc(value).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc_seconds(value: object) -> datetime:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is None
    ):
        raise ValueError("timestamp is not canonical UTC seconds")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)
