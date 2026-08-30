"""ADMIN authorization and immutable prompt revision service tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

import shittim_records.admin as admin_module
from shittim_records.admin import (
    SYSTEM_PROMPT_CONFIRMATION,
    AdminAuthorizer,
    AdminFailure,
    AdminPromptService,
    AdminSecurityConfiguration,
    PromptHistoryPage,
    PromptOperation,
    PromptRevision,
    PromptRevisionIncomplete,
    PromptRevisionSummary,
    PromptValues,
    aggregate_checksum,
    manifest_json,
    normalize_prompt,
    parse_manifest,
)
from shittim_records.archive import derive_requester_key
from shittim_records.auth import SessionRecord, csrf_hash, session_hash

NOW = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
REVISION = "r01k3gqp6g00000000000000000"
REVISION_TWO = "r01k3gqp6g00000000000000001"
REVISION_THREE = "r01k3gqp6g00000000000000002"
ADMIN_ID = "123456789" + "012345678"
IDENTITY_KEY = b"i" * 32
SESSION_KEY = b"s" * 32


def prompt_values(*, system: str = "system") -> PromptValues:
    return PromptValues.from_mapping(
        {
            "system": system,
            "moderator": "moderator",
            "participant-a": "a",
            "participant-b": "b",
            "participant-c": "c",
        }
    )


class SessionStore:
    def __init__(self, record: SessionRecord | None) -> None:
        self.record = record
        self.received_hash: str | None = None

    def get_session(self, *, session_hash: str) -> SessionRecord | None:
        self.received_hash = session_hash
        return self.record


class RevisionStore:
    def __init__(self) -> None:
        self.active: str | None = None
        self.revisions: dict[str, PromptRevision] = {}
        self.incomplete: set[str] = set()

    def load_active_revision_id(self) -> str | None:
        return self.active

    def load_revision(self, revision: str) -> PromptRevision:
        if revision in self.incomplete:
            raise PromptRevisionIncomplete
        return self.revisions[revision]

    def create_revision(self, revision: PromptRevision) -> None:
        existing = self.revisions.setdefault(revision.manifest.revision, revision)
        if existing != revision:
            raise AdminFailure("PROMPT_CONFIGURATION_CONFLICT", 409)

    def activate(self, *, revision: str, expected_base_revision: str | None) -> None:
        if self.active == revision:
            assert self.revisions[revision].manifest.base_revision == expected_base_revision
            return
        if self.active != expected_base_revision:
            raise AdminFailure("PROMPT_REVISION_CONFLICT", 409)
        self.active = revision

    def delete_revision(self, revision: str) -> None:
        if revision == self.active:
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        self.revisions.pop(revision, None)


class Legacy:
    def __init__(self, prompts: PromptValues) -> None:
        self.prompts = prompts

    def load(self) -> PromptValues:
        return self.prompts


class Audit:
    def __init__(self, *, fail_first_completion: bool = False) -> None:
        self.operations: dict[str, PromptOperation] = {}
        self.summaries: dict[str, PromptRevisionSummary] = {}
        self.fail_first_completion = fail_first_completion
        self.pending_revision: str | None = None
        self.pending_request_hash: str | None = None
        self.pending_idempotency_hash: str | None = None
        self.active_revision: str | None = None

    def get_operation(self, idempotency_hash: str) -> PromptOperation | None:
        return self.operations.get(idempotency_hash)

    def get_pending_operation(self, request_hash: str) -> PromptOperation | None:
        if self.pending_revision is None:
            return None
        if self.pending_request_hash != request_hash:
            raise AdminFailure("PROMPT_REVISION_CONFLICT", 409)
        assert self.pending_idempotency_hash is not None
        return self.operations[self.pending_idempotency_hash]

    def get_pending_for_active_revision(self, revision: str) -> PromptOperation | None:
        if self.pending_revision != revision:
            return None
        assert self.pending_idempotency_hash is not None
        return self.operations[self.pending_idempotency_hash]

    def get_pending_operation_any(self) -> PromptOperation | None:
        if self.pending_revision is None:
            return None
        assert self.pending_idempotency_hash is not None
        return self.operations[self.pending_idempotency_hash]

    def begin_operation(
        self,
        *,
        idempotency_hash: str,
        request_hash: str,
        revision: str,
        created_at: datetime,
        action: admin_module.PromptAction,
        expected_base_revision: str | None,
        source_revision: str | None,
    ) -> PromptOperation:
        if self.pending_revision is not None or self.active_revision != expected_base_revision:
            raise AdminFailure("PROMPT_REVISION_CONFLICT", 409)
        operation = PromptOperation(
            idempotency_hash=idempotency_hash,
            request_hash=request_hash,
            revision=revision,
            created_at=created_at,
            action=action,
            base_revision=expected_base_revision,
            source_revision=source_revision,
            complete=False,
        )
        self.operations[idempotency_hash] = operation
        self.pending_revision = revision
        self.pending_request_hash = request_hash
        self.pending_idempotency_hash = idempotency_hash
        return operation

    def complete_operation(
        self,
        *,
        operation: PromptOperation,
        summary: PromptRevisionSummary,
    ) -> None:
        if self.fail_first_completion:
            self.fail_first_completion = False
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503)
        self.summaries[summary.revision] = summary
        self.operations[operation.idempotency_hash] = PromptOperation(
            idempotency_hash=operation.idempotency_hash,
            request_hash=operation.request_hash,
            revision=operation.revision,
            created_at=operation.created_at,
            action=operation.action,
            base_revision=operation.base_revision,
            source_revision=operation.source_revision,
            complete=True,
        )
        self.active_revision = operation.revision
        self.pending_revision = None
        self.pending_request_hash = None
        self.pending_idempotency_hash = None

    def abort_operation(self, *, operation: PromptOperation) -> None:
        assert self.pending_revision == operation.revision
        self.operations.pop(operation.idempotency_hash)
        self.pending_revision = None
        self.pending_request_hash = None
        self.pending_idempotency_hash = None

    def get_summary(self, revision: str) -> PromptRevisionSummary | None:
        return self.summaries.get(revision)

    def list_summaries(self, *, limit: int, cursor: str | None) -> PromptHistoryPage:
        ordered = sorted(self.summaries.values(), key=lambda item: item.revision, reverse=True)
        start = 0
        if cursor is not None:
            try:
                start = next(
                    index + 1 for index, summary in enumerate(ordered) if summary.revision == cursor
                )
            except StopIteration:
                raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503) from None
        items = tuple(ordered[start : start + limit])
        next_cursor = items[-1].revision if start + len(items) < len(ordered) else None
        return PromptHistoryPage(items=items, next_cursor=next_cursor)

    def delete_summary(self, revision: str) -> None:
        self.summaries.pop(revision, None)


def session(requester_key: str) -> SessionRecord:
    return SessionRecord(
        requester_key=requester_key,
        display_name="private-name",
        avatar_asset_key=None,
        csrf_hash=csrf_hash(SESSION_KEY, "csrf-token"),
        guild_verified_at=NOW.isoformat(),
        expires_at=int((NOW + timedelta(hours=1)).timestamp()),
    )


def test_authorizer_authenticates_members_and_restricts_prompt_writes() -> None:
    expected = derive_requester_key(IDENTITY_KEY, ADMIN_ID)
    store = SessionStore(session(expected))
    configuration = AdminSecurityConfiguration(
        identity_hmac_key=IDENTITY_KEY,
        session_hmac_key=SESSION_KEY,
        admin_discord_user_id=ADMIN_ID,
        allowed_origin="https://records.example.invalid",
    )
    authorizer = AdminAuthorizer(
        store=store,
        configuration=configuration,
    )

    authenticated = authorizer.authenticate(raw_session="session-token", now=NOW)

    assert authenticated.requester_key == expected
    assert store.received_hash == session_hash(SESSION_KEY, "session-token")
    assert ADMIN_ID not in repr(authorizer)
    assert (
        authorizer.authorize_write(
            session=authenticated,
            raw_csrf="csrf-token",
            csrf_header="csrf-token",
            origin="https://records.example.invalid",
            idempotency_key="idempotency-key-1",
        )
        == hashlib.sha256(b"idempotency-key-1").hexdigest()
    )

    member = AdminAuthorizer(
        store=SessionStore(session(derive_requester_key(IDENTITY_KEY, "999999999" + "999999999"))),
        configuration=configuration,
    )
    member_session = member.authenticate(raw_session="session-token", now=NOW)

    refresh_key = member.authorize_status_refresh(
        session=member_session,
        raw_csrf="csrf-token",
        csrf_header="csrf-token",
        origin="https://records.example.invalid",
        idempotency_key="idempotency-key-1",
    )
    assert refresh_key == hashlib.sha256(b"idempotency-key-1").hexdigest()

    with pytest.raises(AdminFailure) as caught:
        member.authorize_write(
            session=member_session,
            raw_csrf="csrf-token",
            csrf_header="csrf-token",
            origin="https://records.example.invalid",
            idempotency_key="idempotency-key-1",
        )
    assert caught.value.code == "ADMIN_ACCESS_DENIED"
    assert caught.value.status == 403


def test_prompt_normalization_manifest_and_byte_limit_are_strict() -> None:
    prompts = prompt_values(system="e\N{COMBINING ACUTE ACCENT}\r\nline")
    assert prompts.system == "\N{LATIN SMALL LETTER E WITH ACUTE}\nline"
    manifest = admin_module.PromptManifest(
        revision=REVISION,
        created_at=NOW,
        action="publish",
        base_revision=None,
        checksums=prompts.checksums(),
    )
    assert parse_manifest(manifest_json(manifest)) == manifest
    assert len(aggregate_checksum(manifest.checksums)) == 64
    assert normalize_prompt("a" * 3_500) == "a" * 3_500
    assert len(normalize_prompt("あ" * 1_166 + "a").encode()) == 3_499
    assert len(normalize_prompt("あ" * 1_166 + "aa").encode()) == 3_500
    with pytest.raises(AdminFailure):
        normalize_prompt("あ" * 1_166 + "aaa")
    for blank in ("", " \n\t"):
        with pytest.raises(AdminFailure):
            normalize_prompt(blank)


def test_legacy_registration_allows_same_content_then_managed_noop_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_module, "new_revision_id", lambda _now: REVISION)
    revisions = RevisionStore()
    service = AdminPromptService(
        revisions=revisions,
        legacy=Legacy(prompt_values()),
        audit=Audit(),
    )

    first = service.apply(
        base_revision=None,
        prompts=prompt_values().as_mapping(),
        system_confirmation=None,
        idempotency_hash="a" * 64,
        now=NOW,
    )

    assert first.revision == REVISION
    assert (
        service.apply(
            base_revision=None,
            prompts=prompt_values().as_mapping(),
            system_confirmation=None,
            idempotency_hash="a" * 64,
            now=NOW + timedelta(seconds=1),
        )
        == first
    )
    with pytest.raises(AdminFailure) as reused_key:
        service.apply(
            base_revision=REVISION,
            prompts=prompt_values(system="different").as_mapping(),
            system_confirmation=SYSTEM_PROMPT_CONFIRMATION,
            idempotency_hash="a" * 64,
            now=NOW + timedelta(seconds=2),
        )
    assert reused_key.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(AdminFailure) as caught:
        service.apply(
            base_revision=REVISION,
            prompts=prompt_values().as_mapping(),
            system_confirmation=None,
            idempotency_hash="b" * 64,
            now=NOW + timedelta(seconds=3),
        )
    assert caught.value.code == "PROMPT_CONTENT_UNCHANGED"
    assert caught.value.status == 409


def test_unicode_system_prompt_comparison_requires_confirmation_without_type_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revisions_to_create = iter((REVISION, REVISION_TWO))
    monkeypatch.setattr(admin_module, "new_revision_id", lambda _now: next(revisions_to_create))
    revisions = RevisionStore()
    service = AdminPromptService(
        revisions=revisions,
        legacy=Legacy(prompt_values(system="既存のシステムプロンプト")),
        audit=Audit(),
    )

    service.apply(
        base_revision=None,
        prompts=prompt_values(system="既存のシステムプロンプト").as_mapping(),
        system_confirmation=None,
        idempotency_hash="a" * 64,
        now=NOW,
    )
    with pytest.raises(AdminFailure) as missing_confirmation:
        service.apply(
            base_revision=REVISION,
            prompts=prompt_values(system="更新後のシステムプロンプト").as_mapping(),
            system_confirmation=None,
            idempotency_hash="b" * 64,
            now=NOW + timedelta(seconds=1),
        )
    assert missing_confirmation.value.code == "SYSTEM_PROMPT_CONFIRMATION_REQUIRED"

    result = service.apply(
        base_revision=REVISION,
        prompts=prompt_values(system="更新後のシステムプロンプト").as_mapping(),
        system_confirmation=SYSTEM_PROMPT_CONFIRMATION,
        idempotency_hash="c" * 64,
        now=NOW + timedelta(seconds=2),
    )
    assert result.revision == REVISION_TWO


@pytest.mark.parametrize(
    ("origin", "csrf_header", "idempotency_key", "expected_code"),
    (
        ("https://other.example.invalid", "csrf-token", "idempotency-key-1", "ORIGIN_INVALID"),
        (
            "https://records.example.invalid",
            "wrong-csrf",
            "idempotency-key-1",
            "CSRF_INVALID",
        ),
        (
            "https://records.example.invalid",
            "csrf-token",
            "short",
            "IDEMPOTENCY_KEY_INVALID",
        ),
    ),
)
def test_admin_writes_require_exact_origin_csrf_and_idempotency(
    origin: str,
    csrf_header: str,
    idempotency_key: str,
    expected_code: str,
) -> None:
    configuration = AdminSecurityConfiguration(
        identity_hmac_key=IDENTITY_KEY,
        session_hmac_key=SESSION_KEY,
        admin_discord_user_id=ADMIN_ID,
        allowed_origin="https://records.example.invalid",
    )
    authorizer = AdminAuthorizer(
        store=SessionStore(None),
        configuration=configuration,
    )

    with pytest.raises(AdminFailure) as caught:
        authorizer.authorize_write(
            session=session(derive_requester_key(IDENTITY_KEY, ADMIN_ID)),
            raw_csrf="csrf-token",
            csrf_header=csrf_header,
            origin=origin,
            idempotency_key=idempotency_key,
        )

    assert caught.value.code == expected_code


def test_retry_after_pointer_activation_before_audit_completion_converges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_module, "new_revision_id", lambda _now: REVISION)
    revisions = RevisionStore()
    audit = Audit(fail_first_completion=True)
    service = AdminPromptService(
        revisions=revisions,
        legacy=Legacy(prompt_values()),
        audit=audit,
    )
    proposed = prompt_values(system="updated")

    with pytest.raises(AdminFailure) as first:
        service.apply(
            base_revision=None,
            prompts=proposed.as_mapping(),
            system_confirmation=SYSTEM_PROMPT_CONFIRMATION,
            idempotency_hash="a" * 64,
            now=NOW,
        )
    assert first.value.code == "PROMPT_CONFIGURATION_UNAVAILABLE"
    assert revisions.active == REVISION
    assert audit.operations["a" * 64].complete is False

    result = service.apply(
        base_revision=None,
        prompts=proposed.as_mapping(),
        system_confirmation=SYSTEM_PROMPT_CONFIRMATION,
        idempotency_hash="b" * 64,
        now=NOW + timedelta(seconds=1),
    )

    assert result.revision == REVISION
    assert audit.operations["a" * 64].complete is True
    assert audit.active_revision == REVISION


def test_new_idempotency_key_with_different_pending_payload_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_module, "new_revision_id", lambda _now: REVISION)
    revisions = RevisionStore()
    audit = Audit(fail_first_completion=True)
    service = AdminPromptService(
        revisions=revisions,
        legacy=Legacy(prompt_values()),
        audit=audit,
    )
    with pytest.raises(AdminFailure):
        service.apply(
            base_revision=None,
            prompts=prompt_values(system="first").as_mapping(),
            system_confirmation=SYSTEM_PROMPT_CONFIRMATION,
            idempotency_hash="a" * 64,
            now=NOW,
        )

    with pytest.raises(AdminFailure) as caught:
        service.apply(
            base_revision=None,
            prompts=prompt_values(system="different").as_mapping(),
            system_confirmation=SYSTEM_PROMPT_CONFIRMATION,
            idempotency_hash="b" * 64,
            now=NOW + timedelta(seconds=1),
        )

    assert caught.value.code == "PROMPT_REVISION_CONFLICT"


def test_get_current_recovers_pointer_switched_operation_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revisions_to_create = iter((REVISION, REVISION_TWO))
    monkeypatch.setattr(admin_module, "new_revision_id", lambda _now: next(revisions_to_create))
    revisions = RevisionStore()
    audit = Audit(fail_first_completion=True)
    service = AdminPromptService(
        revisions=revisions,
        legacy=Legacy(prompt_values()),
        audit=audit,
    )

    with pytest.raises(AdminFailure):
        service.apply(
            base_revision=None,
            prompts=prompt_values(system="first").as_mapping(),
            system_confirmation=SYSTEM_PROMPT_CONFIRMATION,
            idempotency_hash="a" * 64,
            now=NOW,
        )

    current = service.get_current()

    assert current.revision is not None
    assert current.revision.manifest.revision == REVISION
    assert audit.operations["a" * 64].complete is True
    assert audit.pending_revision is None

    second = service.apply(
        base_revision=REVISION,
        prompts=prompt_values(system="second").as_mapping(),
        system_confirmation=SYSTEM_PROMPT_CONFIRMATION,
        idempotency_hash="b" * 64,
        now=NOW + timedelta(seconds=1),
    )

    assert second.revision == REVISION_TWO
    assert audit.operations["b" * 64].complete is True


def test_get_current_finishes_revision_created_before_pointer_switch() -> None:
    revisions = RevisionStore()
    audit = Audit()
    proposed = prompt_values(system="updated")
    operation = audit.begin_operation(
        idempotency_hash="a" * 64,
        request_hash="1" * 64,
        revision=REVISION,
        created_at=NOW,
        action="publish",
        expected_base_revision=None,
        source_revision=None,
    )
    revision = PromptRevision(
        manifest=admin_module.PromptManifest(
            revision=REVISION,
            created_at=NOW,
            action="publish",
            base_revision=None,
            checksums=proposed.checksums(),
        ),
        prompts=proposed,
    )
    revisions.create_revision(revision)
    service = AdminPromptService(
        revisions=revisions,
        legacy=Legacy(prompt_values()),
        audit=audit,
    )

    current = service.get_current()

    assert current.revision == revision
    assert revisions.active == REVISION
    assert audit.operations[operation.idempotency_hash].complete is True
    assert audit.pending_revision is None


def test_get_current_aborts_incomplete_inactive_revision_and_allows_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revisions = RevisionStore()
    revisions.incomplete.add(REVISION)
    audit = Audit()
    audit.begin_operation(
        idempotency_hash="a" * 64,
        request_hash="1" * 64,
        revision=REVISION,
        created_at=NOW,
        action="publish",
        expected_base_revision=None,
        source_revision=None,
    )
    service = AdminPromptService(
        revisions=revisions,
        legacy=Legacy(prompt_values()),
        audit=audit,
    )

    current = service.get_current()

    assert current.mode == "legacy"
    assert audit.pending_revision is None
    assert "a" * 64 not in audit.operations

    monkeypatch.setattr(admin_module, "new_revision_id", lambda _now: REVISION_TWO)
    result = service.apply(
        base_revision=None,
        prompts=prompt_values(system="retry").as_mapping(),
        system_confirmation=SYSTEM_PROMPT_CONFIRMATION,
        idempotency_hash="b" * 64,
        now=NOW + timedelta(seconds=1),
    )
    assert result.revision == REVISION_TWO


def test_rollback_rejects_untracked_orphan_revision() -> None:
    revisions = RevisionStore()
    source_prompts = prompt_values(system="orphan")
    revisions.revisions[REVISION] = PromptRevision(
        manifest=admin_module.PromptManifest(
            revision=REVISION,
            created_at=NOW,
            action="publish",
            base_revision=None,
            checksums=source_prompts.checksums(),
        ),
        prompts=source_prompts,
    )
    service = AdminPromptService(
        revisions=revisions,
        legacy=Legacy(prompt_values()),
        audit=Audit(),
    )

    with pytest.raises(AdminFailure) as caught:
        service.rollback(
            base_revision=REVISION,
            source_revision=REVISION,
            system_confirmation=SYSTEM_PROMPT_CONFIRMATION,
            idempotency_hash="a" * 64,
            now=NOW,
        )

    assert caught.value.code == "PROMPT_REVISION_NOT_FOUND"


def test_revision_detail_rejects_audit_checksum_mismatch() -> None:
    revisions = RevisionStore()
    value = prompt_values()
    revisions.revisions[REVISION] = PromptRevision(
        manifest=admin_module.PromptManifest(
            revision=REVISION,
            created_at=NOW,
            action="publish",
            base_revision=None,
            checksums=value.checksums(),
        ),
        prompts=value,
    )
    audit = Audit()
    audit.summaries[REVISION] = PromptRevisionSummary(
        revision=REVISION,
        created_at=NOW,
        action="publish",
        base_revision=None,
        source_revision=None,
        checksum="0" * 64,
    )
    service = AdminPromptService(revisions=revisions, legacy=Legacy(value), audit=audit)

    with pytest.raises(AdminFailure) as caught:
        service.get_revision(REVISION)

    assert caught.value.code == "PROMPT_CONFIGURATION_INVALID"


def test_rollback_copies_a_tracked_revision_into_a_new_immutable_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = iter((REVISION, REVISION_TWO, REVISION_THREE))
    monkeypatch.setattr(admin_module, "new_revision_id", lambda _now: next(generated))
    revisions = RevisionStore()
    audit = Audit()
    legacy = prompt_values(system="legacy system")
    service = AdminPromptService(revisions=revisions, legacy=Legacy(legacy), audit=audit)

    first = service.apply(
        base_revision=None,
        prompts=legacy.as_mapping(),
        system_confirmation=None,
        idempotency_hash="a" * 64,
        now=NOW,
    )
    second = service.apply(
        base_revision=first.revision,
        prompts=prompt_values(system="new system").as_mapping(),
        system_confirmation=SYSTEM_PROMPT_CONFIRMATION,
        idempotency_hash="b" * 64,
        now=NOW + timedelta(seconds=1),
    )

    restored = service.rollback(
        base_revision=second.revision,
        source_revision=first.revision,
        system_confirmation=SYSTEM_PROMPT_CONFIRMATION,
        idempotency_hash="c" * 64,
        now=NOW + timedelta(seconds=2),
    )

    assert restored.revision == REVISION_THREE
    assert restored.action == "rollback"
    assert restored.base_revision == REVISION_TWO
    assert restored.source_revision == REVISION
    assert revisions.revisions[REVISION_THREE].prompts == legacy
    assert revisions.revisions[REVISION_THREE].manifest.base_revision == REVISION_TWO


def test_revision_retention_keeps_active_and_four_previous_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = tuple(f"r01k3gqp6g0000000000000000{index}" for index in range(6))
    generated_iter = iter(generated)
    monkeypatch.setattr(admin_module, "new_revision_id", lambda _now: next(generated_iter))
    revisions = RevisionStore()
    audit = Audit()
    initial = prompt_values(system="value-0")
    service = AdminPromptService(revisions=revisions, legacy=Legacy(initial), audit=audit)

    base: str | None = None
    for index in range(6):
        result = service.apply(
            base_revision=base,
            prompts=prompt_values(system=f"value-{index}").as_mapping(),
            system_confirmation=None if index == 0 else SYSTEM_PROMPT_CONFIRMATION,
            idempotency_hash=f"{index}" * 64,
            now=NOW + timedelta(seconds=index),
        )
        base = result.revision

    assert revisions.active == generated[-1]
    assert set(revisions.revisions) == set(generated[1:])
    assert set(audit.summaries) == set(generated[1:])
    assert generated[0] not in revisions.revisions
    assert generated[0] not in audit.summaries


def test_retention_follows_the_active_chain_instead_of_revision_sort_order() -> None:
    revisions = RevisionStore()
    audit = Audit()
    value = prompt_values()
    active = PromptRevision(
        manifest=admin_module.PromptManifest(
            revision=REVISION,
            created_at=NOW,
            action="publish",
            base_revision=None,
            checksums=value.checksums(),
        ),
        prompts=value,
    )
    revisions.revisions[REVISION] = active
    revisions.active = REVISION
    audit.summaries[REVISION_TWO] = PromptRevisionSummary(
        revision=REVISION_TWO,
        created_at=NOW + timedelta(seconds=1),
        action="publish",
        base_revision=REVISION,
        source_revision=None,
        checksum="f" * 64,
    )
    audit.summaries[REVISION] = PromptRevisionSummary(
        revision=REVISION,
        created_at=NOW,
        action="publish",
        base_revision=None,
        source_revision=None,
        checksum=aggregate_checksum(value.checksums()),
    )
    service = AdminPromptService(revisions=revisions, legacy=Legacy(value), audit=audit)

    current = service.get_current()

    assert current.revision == active
    assert revisions.active == REVISION
    assert REVISION in revisions.revisions
    assert REVISION in audit.summaries
    assert REVISION_TWO not in audit.summaries


def test_retention_retries_after_revision_body_cleanup_precedes_summary_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailOnceAudit(Audit):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_delete = True

        def delete_summary(self, revision: str) -> None:
            if self.fail_next_delete:
                self.fail_next_delete = False
                raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503)
            super().delete_summary(revision)

    generated = tuple(f"r01k3gqp6g0000000000000000{index}" for index in range(6))
    generated_iter = iter(generated)
    monkeypatch.setattr(admin_module, "new_revision_id", lambda _now: next(generated_iter))
    revisions = RevisionStore()
    audit = FailOnceAudit()
    service = AdminPromptService(
        revisions=revisions,
        legacy=Legacy(prompt_values(system="value-0")),
        audit=audit,
    )

    base: str | None = None
    for index in range(5):
        base = service.apply(
            base_revision=base,
            prompts=prompt_values(system=f"value-{index}").as_mapping(),
            system_confirmation=None if index == 0 else SYSTEM_PROMPT_CONFIRMATION,
            idempotency_hash=f"{index}" * 64,
            now=NOW + timedelta(seconds=index),
        ).revision

    with pytest.raises(AdminFailure) as caught:
        service.apply(
            base_revision=base,
            prompts=prompt_values(system="value-5").as_mapping(),
            system_confirmation=SYSTEM_PROMPT_CONFIRMATION,
            idempotency_hash="5" * 64,
            now=NOW + timedelta(seconds=5),
        )

    assert caught.value.code == "PROMPT_CONFIGURATION_UNAVAILABLE"
    assert revisions.active == generated[-1]
    assert generated[0] not in revisions.revisions
    assert generated[0] in audit.summaries

    service.get_current()

    assert generated[0] not in audit.summaries
    assert set(revisions.revisions) == set(generated[1:])


def test_same_base_revision_cannot_hold_two_pending_updates() -> None:
    audit = Audit()
    audit.begin_operation(
        idempotency_hash="a" * 64,
        request_hash="1" * 64,
        revision=REVISION,
        created_at=NOW,
        action="publish",
        expected_base_revision=None,
        source_revision=None,
    )

    with pytest.raises(AdminFailure) as caught:
        audit.begin_operation(
            idempotency_hash="b" * 64,
            request_hash="2" * 64,
            revision="r01k3gqp6g00000000000000001",
            created_at=NOW,
            action="publish",
            expected_base_revision=None,
            source_revision=None,
        )

    assert caught.value.code == "PROMPT_REVISION_CONFLICT"
