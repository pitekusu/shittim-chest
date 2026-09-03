"""Pure domain tests for the owner-scoped Memorial Lobby workflow."""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from shittim_records.auth import SessionRecord, csrf_hash, session_hash
from shittim_records.contracts import MemorialStateName
from shittim_records.memorial import (
    GENERATE_CONFIRMATION,
    RESET_CONFIRMATION,
    GeneratedMemorialImage,
    MemorialAuthorizer,
    MemorialFailure,
    MemorialGenerationJob,
    MemorialGenerationService,
    MemorialMemory,
    MemorialSecurityConfiguration,
    MemorialService,
    MemorialSnapshot,
    MemorialUploadReservation,
    MemorialUploadTicket,
    achieved_date,
)

NOW = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)
REQUESTER_KEY = "a" * 43
SHA256 = "b" * 64


def snapshot(state: MemorialStateName = "unlocked") -> MemorialSnapshot:
    return MemorialSnapshot(
        requester_key=REQUESTER_KEY,
        state=state,
        cycle=1,
        reset_count=0,
        unlocked_participant=None if state == "locked" else "participant-a",
        unlocked_at=None if state == "locked" else NOW,
    )


def reservation() -> MemorialUploadReservation:
    return MemorialUploadReservation(
        requester_key=REQUESTER_KEY,
        cycle=1,
        asset_key="private/upload",
        content_type="image/png",
        size_bytes=1234,
        sha256=SHA256,
        expires_at=NOW + timedelta(minutes=5),
    )


def upload_fields() -> dict[str, str]:
    return {
        "key": "private/upload",
        "Content-Type": "image/png",
        "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(SHA256)).decode(),
        "x-amz-algorithm": "AWS4-HMAC-SHA256",
        "x-amz-credential": "credential/scope",
        "x-amz-date": "20260903T010000Z",
        "policy": "cG9saWN5",
        "x-amz-signature": "c" * 64,
    }


class SessionStore:
    def __init__(self, record: SessionRecord | None) -> None:
        self.record = record
        self.requested_hash: str | None = None

    def get_session(self, *, session_hash: str) -> SessionRecord | None:
        self.requested_hash = session_hash
        return self.record


def test_authorizer_binds_writes_to_authenticated_session() -> None:
    key = b"k" * 32
    raw_csrf = "csrf-secret"
    record = SessionRecord(
        requester_key=REQUESTER_KEY,
        display_name="利用者",
        avatar_asset_key=None,
        csrf_hash=csrf_hash(key, raw_csrf),
        guild_verified_at=NOW.isoformat(),
        expires_at=int((NOW + timedelta(hours=1)).timestamp()),
    )
    store = SessionStore(record)
    authorizer = MemorialAuthorizer(
        store=store,
        configuration=MemorialSecurityConfiguration(
            session_hmac_key=key,
            allowed_origin="https://records.example.invalid",
        ),
    )

    session = authorizer.authenticate(raw_session="browser-session", now=NOW)
    digest = authorizer.authorize_write(
        session=session,
        raw_csrf=raw_csrf,
        csrf_header=raw_csrf,
        origin="https://records.example.invalid",
        idempotency_key="idempotency-key-1",
    )

    assert session is record
    assert store.requested_hash == session_hash(key, "browser-session")
    assert len(digest) == 64
    with pytest.raises(MemorialFailure, match="ORIGIN_INVALID"):
        authorizer.authorize_write(
            session=session,
            raw_csrf=raw_csrf,
            csrf_header=raw_csrf,
            origin="https://lookalike.example.invalid",
            idempotency_key="idempotency-key-1",
        )

    for invalid in (
        {"raw_csrf": "wrong", "csrf_header": "wrong", "idempotency_key": "idempotency-key-1"},
        {"raw_csrf": raw_csrf, "csrf_header": raw_csrf, "idempotency_key": "too-short"},
    ):
        with pytest.raises(MemorialFailure):
            authorizer.authorize_write(
                session=session,
                origin="https://records.example.invalid",
                **invalid,
            )


class Repository:
    def __init__(self, current: MemorialSnapshot | None = None) -> None:
        self.current = current or snapshot()
        self.upload = reservation()
        self.queue_calls = 0
        self.reset_calls = 0
        self.failed_job: MemorialGenerationJob | None = None
        self.reserve_calls = 0

    def get_snapshot(self, *, requester_key: str) -> MemorialSnapshot:
        assert requester_key == REQUESTER_KEY
        return self.current

    def reserve_upload(self, **kwargs: Any) -> MemorialUploadReservation:
        assert kwargs["requester_key"] == REQUESTER_KEY
        assert kwargs["expected_cycle"] == 1
        assert kwargs["idempotency_hash"] == "d" * 64
        self.reserve_calls += 1
        return self.upload

    def get_upload(self, *, requester_key: str, cycle: int) -> MemorialUploadReservation | None:
        assert (requester_key, cycle) == (REQUESTER_KEY, 1)
        return self.upload

    def get_failed_generation(self, **kwargs: Any) -> MemorialGenerationJob | None:
        del kwargs
        return self.failed_job

    def queue_generation(self, **kwargs: Any) -> MemorialSnapshot:
        assert kwargs["requester_key"] == REQUESTER_KEY
        assert kwargs["expected_cycle"] == 1
        self.queue_calls += 1
        self.current = snapshot("queued")
        return self.current

    def get_memory(self, **kwargs: Any) -> None:
        del kwargs
        return None

    def reset_affection(self, **kwargs: Any) -> MemorialSnapshot:
        assert kwargs["expected_cycle"] == 1
        assert kwargs["reset_score"] == 500
        self.reset_calls += 1
        self.current = MemorialSnapshot(
            requester_key=REQUESTER_KEY,
            state="locked",
            cycle=2,
            reset_count=1,
            unlocked_participant=None,
            unlocked_at=None,
        )
        return self.current


class Assets:
    def __init__(self) -> None:
        self.verified = True
        self.deleted_reservations = 0
        self.existing: str | None = None

    def create_upload_ticket(self, reservation: MemorialUploadReservation) -> MemorialUploadTicket:
        return MemorialUploadTicket(
            upload_url="https://upload.example.invalid/",
            expires_at=reservation.expires_at,
            fields=upload_fields(),
        )

    def verify_upload(self, reservation: MemorialUploadReservation) -> bool:
        assert reservation == reservation
        return self.verified

    def existing_generated(self, job: MemorialGenerationJob) -> str | None:
        return self.existing

    def delete_reservation(self, reservation: MemorialUploadReservation) -> None:
        assert reservation.asset_key == "private/upload"
        self.deleted_reservations += 1


class Queue:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.sent: list[tuple[str, int]] = []

    def send(self, *, requester_key: str, cycle: int) -> None:
        self.sent.append((requester_key, cycle))
        if self.failure is not None:
            raise self.failure


def test_service_uses_presigned_post_and_resends_idempotent_queue_checkpoint() -> None:
    repository = Repository()
    queue = Queue()
    service = MemorialService(
        repository=cast(Any, repository),
        assets=cast(Any, Assets()),
        queue=queue,
    )

    cycle, ticket = service.prepare_upload(
        requester_key=REQUESTER_KEY,
        expected_cycle=1,
        content_type="image/png",
        size_bytes=1234,
        sha256=SHA256,
        idempotency_hash="d" * 64,
        now=NOW,
    )
    first = service.queue_generation(
        requester_key=REQUESTER_KEY,
        expected_cycle=1,
        confirmation=GENERATE_CONFIRMATION,
        idempotency_hash="d" * 64,
        now=NOW,
    )
    second = service.queue_generation(
        requester_key=REQUESTER_KEY,
        expected_cycle=1,
        confirmation=GENERATE_CONFIRMATION,
        idempotency_hash="e" * 64,
        now=NOW,
    )

    assert cycle == 1
    assert ticket.fields["Content-Type"] == "image/png"
    assert first.state == second.state == "queued"
    assert repository.queue_calls == 2
    assert queue.sent == [(REQUESTER_KEY, 1), (REQUESTER_KEY, 1)]


@pytest.mark.parametrize("operation", ("upload", "generate", "reset"))
def test_stale_write_cannot_mutate_a_later_cycle(operation: str) -> None:
    repository = Repository(
        MemorialSnapshot(
            requester_key=REQUESTER_KEY,
            state="unlocked",
            cycle=2,
            reset_count=1,
            unlocked_participant="participant-b",
            unlocked_at=NOW,
        )
    )
    service = MemorialService(
        repository=cast(Any, repository),
        assets=cast(Any, Assets()),
        queue=Queue(),
    )

    with pytest.raises(MemorialFailure) as failure:
        if operation == "upload":
            service.prepare_upload(
                requester_key=REQUESTER_KEY,
                expected_cycle=1,
                content_type="image/png",
                size_bytes=1234,
                sha256=SHA256,
                idempotency_hash="d" * 64,
                now=NOW,
            )
        elif operation == "generate":
            service.queue_generation(
                requester_key=REQUESTER_KEY,
                expected_cycle=1,
                confirmation=GENERATE_CONFIRMATION,
                idempotency_hash="d" * 64,
                now=NOW,
            )
        else:
            service.reset(
                requester_key=REQUESTER_KEY,
                expected_cycle=1,
                confirmation=RESET_CONFIRMATION,
                idempotency_hash="d" * 64,
                now=NOW,
            )

    assert (failure.value.code, failure.value.status) == ("MEMORIAL_STATE_CONFLICT", 409)
    assert (repository.queue_calls, repository.reset_calls) == (0, 0)


def test_failed_generation_with_stored_image_must_resume_instead_of_overwrite() -> None:
    repository = Repository(snapshot("failed"))
    failed_job = generation_job(narrative="保存済みの文章")
    repository.failed_job = failed_job
    assets = Assets()
    assets.existing = failed_job.result_asset_key
    service = MemorialService(
        repository=cast(Any, repository),
        assets=cast(Any, assets),
        queue=Queue(),
    )

    with pytest.raises(MemorialFailure) as overwrite:
        service.prepare_upload(
            requester_key=REQUESTER_KEY,
            expected_cycle=1,
            content_type="image/png",
            size_bytes=1234,
            sha256=SHA256,
            idempotency_hash="d" * 64,
            now=NOW,
        )
    assert (overwrite.value.code, overwrite.value.status) == (
        "MEMORIAL_RECOVERY_REQUIRED",
        409,
    )

    resumed = service.queue_generation(
        requester_key=REQUESTER_KEY,
        expected_cycle=1,
        confirmation=GENERATE_CONFIRMATION,
        idempotency_hash="e" * 64,
        now=NOW,
    )
    assert resumed.state == "queued"


@pytest.mark.parametrize("operation", ("upload", "generate"))
def test_terminal_failed_cycle_requires_reset_before_new_paid_work(operation: str) -> None:
    repository = Repository(snapshot("failed"))
    repository.failed_job = generation_job(generation_attempt=3)
    service = MemorialService(
        repository=cast(Any, repository),
        assets=cast(Any, Assets()),
        queue=Queue(),
    )

    with pytest.raises(MemorialFailure) as failure:
        if operation == "upload":
            service.prepare_upload(
                requester_key=REQUESTER_KEY,
                expected_cycle=1,
                content_type="image/png",
                size_bytes=1234,
                sha256=SHA256,
                idempotency_hash="d" * 64,
                now=NOW,
            )
        else:
            service.queue_generation(
                requester_key=REQUESTER_KEY,
                expected_cycle=1,
                confirmation=GENERATE_CONFIRMATION,
                idempotency_hash="d" * 64,
                now=NOW,
            )

    assert (failure.value.code, failure.value.status) == (
        "MEMORIAL_GENERATION_ATTEMPTS_EXHAUSTED",
        409,
    )
    assert repository.reserve_calls == repository.queue_calls == 0


@pytest.mark.parametrize("with_image_checkpoint", (False, True))
def test_failed_generation_with_partial_derived_output_cannot_be_reset(
    with_image_checkpoint: bool,
) -> None:
    repository = Repository(snapshot("failed"))
    repository.failed_job = generation_job(
        narrative="保存済みの文章",
        image_asset_key="private/generated" if with_image_checkpoint else None,
        generation_attempt=3,
    )
    service = MemorialService(
        repository=cast(Any, repository),
        assets=cast(Any, Assets()),
        queue=Queue(),
    )

    with pytest.raises(MemorialFailure) as failure:
        service.reset(
            requester_key=REQUESTER_KEY,
            expected_cycle=1,
            confirmation=RESET_CONFIRMATION,
            idempotency_hash="d" * 64,
            now=NOW,
        )

    assert (failure.value.code, failure.value.status) == (
        "MEMORIAL_RECOVERY_REQUIRED",
        409,
    )
    assert repository.reset_calls == 0


def test_service_maps_queue_failure_and_blocks_reset_while_queued() -> None:
    repository = Repository()
    service = MemorialService(
        repository=cast(Any, repository),
        assets=cast(Any, Assets()),
        queue=Queue(RuntimeError("private provider detail")),
    )

    with pytest.raises(MemorialFailure) as raised:
        service.queue_generation(
            requester_key=REQUESTER_KEY,
            expected_cycle=1,
            confirmation=GENERATE_CONFIRMATION,
            idempotency_hash="d" * 64,
            now=NOW,
        )
    assert (raised.value.code, raised.value.status) == ("MEMORIAL_QUEUE_UNAVAILABLE", 503)
    with pytest.raises(MemorialFailure) as reset_error:
        service.reset(
            requester_key=REQUESTER_KEY,
            expected_cycle=1,
            confirmation=RESET_CONFIRMATION,
            idempotency_hash="d" * 64,
            now=NOW,
        )
    assert (reset_error.value.code, reset_error.value.status) == (
        "MEMORIAL_RESET_NOT_ALLOWED",
        409,
    )
    assert repository.reset_calls == 0


def test_reset_retry_reaches_repository_after_cycle_advanced_to_locked() -> None:
    repository = Repository(
        MemorialSnapshot(
            requester_key=REQUESTER_KEY,
            state="locked",
            cycle=2,
            reset_count=1,
            unlocked_participant=None,
            unlocked_at=None,
        )
    )
    assets = Assets()
    service = MemorialService(
        repository=cast(Any, repository),
        assets=cast(Any, assets),
        queue=Queue(),
    )

    result = service.reset(
        requester_key=REQUESTER_KEY,
        expected_cycle=1,
        confirmation=RESET_CONFIRMATION,
        idempotency_hash="d" * 64,
        now=NOW,
    )

    assert result.cycle == 2
    assert repository.reset_calls == 1
    assert assets.deleted_reservations == 1


def test_reset_cleanup_failure_is_503_and_same_operation_can_retry() -> None:
    class RetryAssets(Assets):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        def delete_reservation(self, reservation: MemorialUploadReservation) -> None:
            if self.fail:
                raise OSError("private storage detail")
            super().delete_reservation(reservation)

    repository = Repository()
    assets = RetryAssets()
    service = MemorialService(
        repository=cast(Any, repository),
        assets=cast(Any, assets),
        queue=Queue(),
    )

    with pytest.raises(MemorialFailure) as failure:
        service.reset(
            requester_key=REQUESTER_KEY,
            expected_cycle=1,
            confirmation=RESET_CONFIRMATION,
            idempotency_hash="d" * 64,
            now=NOW,
        )
    assert (failure.value.code, failure.value.status) == (
        "MEMORIAL_UPLOAD_CLEANUP_FAILED",
        503,
    )
    assets.fail = False
    result = service.reset(
        requester_key=REQUESTER_KEY,
        expected_cycle=1,
        confirmation=RESET_CONFIRMATION,
        idempotency_hash="d" * 64,
        now=NOW,
    )
    assert result.cycle == 2
    assert assets.deleted_reservations == 1


class WorkerRepository:
    def __init__(self, job: MemorialGenerationJob) -> None:
        self.job = job
        self.released = 0
        self.failed = 0
        self.completed = 0
        self.narrative_checkpoints = 0
        self.image_checkpoints = 0
        self.preserve_derived: list[bool] = []

    def claim_generation(self, **kwargs: Any) -> MemorialGenerationJob | None:
        assert kwargs["requester_key"] == REQUESTER_KEY
        assert kwargs["cycle"] == 1
        return self.job

    def checkpoint_narrative(self, **kwargs: Any) -> MemorialGenerationJob:
        self.narrative_checkpoints += 1
        self.job = replace(kwargs["job"], narrative=kwargs["narrative"])
        return self.job

    def checkpoint_image(self, **kwargs: Any) -> MemorialGenerationJob:
        self.image_checkpoints += 1
        self.job = replace(kwargs["job"], image_asset_key=kwargs["image_asset_key"])
        return self.job

    def complete_generation(self, **kwargs: Any) -> MemorialMemory:
        self.completed += 1
        job = kwargs["job"]
        assert job.narrative is not None and job.image_asset_key is not None
        return MemorialMemory(
            cycle=job.cycle,
            participant=job.participant,
            unlocked_at=job.unlocked_at,
            generated_at=kwargs["generated_at"],
            image_asset_key=job.image_asset_key,
            narrative=job.narrative,
        )

    def release_generation_to_queue(self, **kwargs: Any) -> None:
        self.released += 1

    def fail_generation(self, **kwargs: Any) -> None:
        self.failed += 1
        self.preserve_derived.append(kwargs["preserve_derived"])


class WorkerAssets:
    def __init__(self, *, existing: str | None = None) -> None:
        self.existing = existing
        self.loaded = 0
        self.stored = 0
        self.deleted = 0

    def load_upload(self, job: MemorialGenerationJob) -> bytes:
        self.loaded += 1
        return b"source-image"

    def existing_generated(self, job: MemorialGenerationJob) -> str | None:
        return self.existing

    def store_generated(self, **kwargs: Any) -> str:
        self.stored += 1
        return "private/generated"

    def delete_upload(self, job: MemorialGenerationJob) -> None:
        self.deleted += 1


class Questions:
    def latest_questions(self, *, requester_key: str, limit: int) -> tuple[str, ...]:
        assert requester_key == REQUESTER_KEY
        assert limit == 10
        return tuple(f"question-{index}" for index in range(10))


class Generator:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.narratives = 0
        self.images = 0
        self.validations = 0

    def validate_image_inputs(self, **kwargs: Any) -> None:
        assert kwargs["source_image"] == b"source-image"
        self.validations += 1

    def generate_narrative(self, **kwargs: Any) -> str:
        self.narratives += 1
        if self.failure is not None:
            raise self.failure
        assert kwargs["achieved_on"].isoformat() == "2026-09-03"
        return "ふたりの大切な思い出です。"

    def generate_image(self, **kwargs: Any) -> GeneratedMemorialImage:
        self.images += 1
        assert kwargs["narrative"] == "ふたりの大切な思い出です。"
        return GeneratedMemorialImage(image_bytes=b"generated-image")


def generation_job(**overrides: Any) -> MemorialGenerationJob:
    values: dict[str, Any] = {
        "requester_key": REQUESTER_KEY,
        "requester_display_name": "質問者",
        "cycle": 1,
        "participant": "participant-c",
        "unlocked_at": NOW,
        "upload_asset_key": "private/upload",
        "result_asset_key": "private/generated",
        "generation_attempt": 1,
    }
    values.update(overrides)
    return MemorialGenerationJob(**values)


def test_generation_checkpoints_each_paid_call_and_deletes_upload_on_success() -> None:
    repository = WorkerRepository(generation_job())
    assets = WorkerAssets()
    generator = Generator()
    service = MemorialGenerationService(
        repository=cast(Any, repository),
        assets=cast(Any, assets),
        questions=Questions(),
        generator=generator,
        clock=lambda: NOW,
    )

    memory = service.process(
        requester_key=REQUESTER_KEY,
        cycle=1,
        receive_count=1,
        now=NOW,
        deadline=NOW + timedelta(minutes=5),
    )

    assert memory is not None and memory.narrative == "ふたりの大切な思い出です。"
    assert (generator.narratives, generator.images) == (1, 1)
    assert generator.validations == 1
    assert (repository.narrative_checkpoints, repository.image_checkpoints) == (1, 1)
    assert (assets.loaded, assets.stored, assets.deleted) == (1, 1, 1)
    assert repository.completed == 1


def test_generation_retry_reuses_paid_checkpoints() -> None:
    repository = WorkerRepository(
        generation_job(
            narrative="保存済みの文章",
            image_asset_key="private/generated",
        )
    )
    assets = WorkerAssets(existing="private/generated")
    generator = Generator()
    service = MemorialGenerationService(
        repository=cast(Any, repository),
        assets=cast(Any, assets),
        questions=Questions(),
        generator=generator,
        clock=lambda: NOW,
    )

    service.process(
        requester_key=REQUESTER_KEY,
        cycle=1,
        receive_count=2,
        now=NOW,
        deadline=NOW + timedelta(minutes=5),
    )

    assert (generator.narratives, generator.images) == (0, 0)
    assert (assets.loaded, assets.stored, assets.deleted) == (0, 0, 1)


def test_generation_fails_closed_when_checkpointed_image_is_missing() -> None:
    repository = WorkerRepository(
        generation_job(
            narrative="保存済みの文章",
            image_asset_key="private/generated",
            generation_attempt=3,
        )
    )
    assets = WorkerAssets(existing=None)
    generator = Generator()
    service = MemorialGenerationService(
        repository=cast(Any, repository),
        assets=cast(Any, assets),
        questions=Questions(),
        generator=generator,
        clock=lambda: NOW,
    )

    with pytest.raises(MemorialFailure) as captured:
        service.process(
            requester_key=REQUESTER_KEY,
            cycle=1,
            receive_count=1,
            now=NOW,
            deadline=NOW + timedelta(minutes=5),
        )

    assert captured.value.code == "MEMORIAL_GENERATED_ASSET_MISSING"
    assert (generator.narratives, generator.images) == (0, 0)
    assert repository.completed == 0
    assert repository.failed == 1
    assert repository.preserve_derived == [False]


def test_generation_retry_recovers_stored_image_before_another_paid_call() -> None:
    repository = WorkerRepository(generation_job(narrative="保存済みの文章"))
    assets = WorkerAssets(existing="private/generated")
    generator = Generator()
    service = MemorialGenerationService(
        repository=cast(Any, repository),
        assets=cast(Any, assets),
        questions=Questions(),
        generator=generator,
        clock=lambda: NOW,
    )

    service.process(
        requester_key=REQUESTER_KEY,
        cycle=1,
        receive_count=2,
        now=NOW,
        deadline=NOW + timedelta(minutes=5),
    )

    assert (generator.narratives, generator.images) == (0, 0)
    assert (assets.loaded, assets.stored, assets.deleted) == (0, 0, 1)
    assert repository.image_checkpoints == 1


def test_completion_only_recovery_is_allowed_after_paid_attempt_limit() -> None:
    repository = WorkerRepository(generation_job(narrative="保存済みの文章", generation_attempt=4))
    assets = WorkerAssets(existing="private/generated")
    generator = Generator()
    service = MemorialGenerationService(
        repository=cast(Any, repository),
        assets=cast(Any, assets),
        questions=Questions(),
        generator=generator,
        clock=lambda: NOW,
    )

    result = service.process(
        requester_key=REQUESTER_KEY,
        cycle=1,
        receive_count=1,
        now=NOW,
        deadline=NOW + timedelta(minutes=5),
    )

    assert result is not None
    assert (generator.narratives, generator.images) == (0, 0)
    assert (assets.loaded, assets.stored, assets.deleted) == (0, 0, 1)
    assert repository.completed == 1


def test_terminal_image_failure_drops_partial_narrative_when_no_image_was_stored() -> None:
    class ImageFailureGenerator(Generator):
        def generate_image(self, **kwargs: Any) -> GeneratedMemorialImage:
            self.images += 1
            raise RuntimeError("private image provider detail")

    repository = WorkerRepository(generation_job(narrative="保存済みの文章", generation_attempt=3))
    assets = WorkerAssets()
    generator = ImageFailureGenerator()
    service = MemorialGenerationService(
        repository=cast(Any, repository),
        assets=cast(Any, assets),
        questions=Questions(),
        generator=generator,
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="private image provider detail"):
        service.process(
            requester_key=REQUESTER_KEY,
            cycle=1,
            receive_count=1,
            now=NOW,
            deadline=NOW + timedelta(minutes=5),
        )

    assert repository.failed == 1
    assert repository.preserve_derived == [False]
    assert assets.deleted == 1


@pytest.mark.parametrize(
    ("generation_attempt", "receive_count", "released", "failed", "deleted"),
    [(1, 99, 1, 0, 0), (3, 1, 0, 1, 1)],
)
def test_generation_failure_preserves_upload_until_terminal_attempt(
    generation_attempt: int,
    receive_count: int,
    released: int,
    failed: int,
    deleted: int,
) -> None:
    repository = WorkerRepository(generation_job(generation_attempt=generation_attempt))
    assets = WorkerAssets()
    service = MemorialGenerationService(
        repository=cast(Any, repository),
        assets=cast(Any, assets),
        questions=Questions(),
        generator=Generator(RuntimeError("private provider detail")),
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="private provider detail"):
        service.process(
            requester_key=REQUESTER_KEY,
            cycle=1,
            receive_count=receive_count,
            now=NOW,
            deadline=NOW + timedelta(minutes=5),
        )

    assert (repository.released, repository.failed, assets.deleted) == (released, failed, deleted)
    if failed:
        assert repository.preserve_derived == [False]


def test_new_sqs_messages_share_the_persisted_three_attempt_limit() -> None:
    class LogicalAttemptRepository(WorkerRepository):
        def __init__(self) -> None:
            super().__init__(generation_job(generation_attempt=0))

        def claim_generation(self, **kwargs: Any) -> MemorialGenerationJob:
            assert kwargs["requester_key"] == REQUESTER_KEY
            self.job = replace(
                self.job,
                generation_attempt=self.job.generation_attempt + 1,
            )
            return self.job

    repository = LogicalAttemptRepository()
    assets = WorkerAssets()
    service = MemorialGenerationService(
        repository=cast(Any, repository),
        assets=cast(Any, assets),
        questions=Questions(),
        generator=Generator(RuntimeError("private provider detail")),
        clock=lambda: NOW,
    )

    for _new_message in range(3):
        with pytest.raises(RuntimeError, match="private provider detail"):
            service.process(
                requester_key=REQUESTER_KEY,
                cycle=1,
                receive_count=1,
                now=NOW,
                deadline=NOW + timedelta(minutes=5),
            )

    assert repository.job.generation_attempt == 3
    assert (repository.released, repository.failed) == (2, 1)
    assert repository.preserve_derived == [False]
    assert assets.deleted == 1


def test_generation_preflights_images_before_paid_calls_and_stops_near_deadline() -> None:
    repository = WorkerRepository(generation_job())
    assets = WorkerAssets()
    generator = Generator()
    service = MemorialGenerationService(
        repository=cast(Any, repository),
        assets=cast(Any, assets),
        questions=Questions(),
        generator=generator,
        clock=lambda: NOW + timedelta(seconds=46),
    )

    with pytest.raises(MemorialFailure) as failure:
        service.process(
            requester_key=REQUESTER_KEY,
            cycle=1,
            receive_count=1,
            now=NOW,
            deadline=NOW + timedelta(minutes=5),
        )

    assert failure.value.code == "MEMORIAL_GENERATION_DEADLINE"
    assert (generator.validations, generator.narratives, generator.images) == (1, 0, 0)
    assert repository.released == 1


def test_achievement_date_uses_japan_time() -> None:
    assert achieved_date(datetime(2026, 9, 2, 15, 30, tzinfo=UTC)).isoformat() == "2026-09-03"
