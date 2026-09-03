"""Owner-scoped Memorial Lobby domain services and adapter boundaries."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from shittim_records.auth import SessionRecord, csrf_hash, session_hash
from shittim_records.contracts import MemorialStateName, MemorialUploadContentType, ParticipantSlot

GENERATE_CONFIRMATION = "GENERATE MEMORIAL"
RESET_CONFIRMATION = "RESET AFFECTION"
RECENT_QUESTION_LIMIT = 10
MEMORIAL_IMAGE_WIDTH = 1920
MEMORIAL_IMAGE_HEIGHT = 1080
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
AFFECTION_RESET_SCORE = 500
MEMORIAL_PROVIDER_CALL_BUDGET = timedelta(seconds=120)
MEMORIAL_CLEANUP_MARGIN = timedelta(seconds=15)
MEMORIAL_NARRATIVE_START_BUDGET = MEMORIAL_PROVIDER_CALL_BUDGET * 2 + MEMORIAL_CLEANUP_MARGIN
MEMORIAL_IMAGE_START_BUDGET = MEMORIAL_PROVIDER_CALL_BUDGET + MEMORIAL_CLEANUP_MARGIN

_OPAQUE_REQUESTER_KEY = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_POST_REQUIRED_FIELDS = frozenset(
    {
        "key",
        "Content-Type",
        "x-amz-checksum-sha256",
        "x-amz-algorithm",
        "x-amz-credential",
        "x-amz-date",
        "policy",
        "x-amz-signature",
    }
)
_POST_OPTIONAL_FIELDS = frozenset({"x-amz-security-token"})
_TOKYO = ZoneInfo("Asia/Tokyo")


class MemorialFailure(RuntimeError):
    """Stable content-free failure raised at the Memorial HTTP boundary."""

    def __init__(self, code: str, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MemorialSecurityConfiguration:
    session_hmac_key: bytes = field(repr=False)
    allowed_origin: str


@dataclass(frozen=True, slots=True)
class MemorialSnapshot:
    """Current owner-visible state with its private requester identity concealed from repr."""

    requester_key: str = field(repr=False)
    state: MemorialStateName
    cycle: int
    reset_count: int
    unlocked_participant: ParticipantSlot | None
    unlocked_at: datetime | None
    upload_ready: bool = False
    latest_ready_cycle: int | None = None
    memories: tuple[MemorialMemorySummary, ...] = ()

    def __post_init__(self) -> None:
        _require_requester_key(self.requester_key)
        if self.cycle < 1 or self.reset_count < 0 or self.cycle != self.reset_count + 1:
            raise ValueError("memorial cycle must follow reset count")
        if (self.unlocked_participant is None) != (self.unlocked_at is None):
            raise ValueError("memorial unlock metadata must be complete")
        if self.unlocked_at is not None:
            _require_utc(self.unlocked_at, label="memorial unlock timestamp")
        if self.state == "locked" and self.unlocked_participant is not None:
            raise ValueError("locked memorial cannot contain unlock metadata")
        if self.state != "locked" and self.unlocked_participant is None:
            raise ValueError("unlocked memorial requires unlock metadata")
        if self.upload_ready and self.state != "unlocked":
            raise ValueError("only an unlocked memorial can accept its reserved upload")
        if self.latest_ready_cycle is not None and not 1 <= self.latest_ready_cycle <= self.cycle:
            raise ValueError("latest ready memorial cycle is invalid")
        if self.state == "ready" and self.latest_ready_cycle != self.cycle:
            raise ValueError("ready memorial must identify the current cycle")
        cycles = tuple(memory.cycle for memory in self.memories)
        if cycles != tuple(sorted(set(cycles))) or any(cycle > self.cycle for cycle in cycles):
            raise ValueError("memorial summaries must contain unique ascending cycles")
        expected_latest = cycles[-1] if cycles else None
        if self.latest_ready_cycle != expected_latest:
            raise ValueError("latest ready memorial cycle must match the memory summaries")
        if self.state == "ready":
            current = self.memories[-1]
            if (
                current.participant != self.unlocked_participant
                or current.unlocked_at != self.unlocked_at
            ):
                raise ValueError("ready memorial summary must match its unlock")


@dataclass(frozen=True, slots=True)
class MemorialMemorySummary:
    """Content-free metadata used to enumerate an owner's generated memories."""

    cycle: int
    participant: ParticipantSlot
    unlocked_at: datetime
    generated_at: datetime

    def __post_init__(self) -> None:
        if self.cycle < 1:
            raise ValueError("memorial summary cycle is invalid")
        _require_utc(self.unlocked_at, label="memorial unlock timestamp")
        _require_utc(self.generated_at, label="memorial generation timestamp")
        if self.generated_at < self.unlocked_at:
            raise ValueError("memorial generation cannot precede its unlock")


@dataclass(frozen=True, slots=True)
class MemorialUploadReservation:
    """Private immutable upload metadata bound to one owner and affection cycle."""

    requester_key: str = field(repr=False)
    cycle: int
    asset_key: str = field(repr=False)
    content_type: MemorialUploadContentType
    size_bytes: int
    sha256: str
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_requester_key(self.requester_key)
        if self.cycle < 1 or not self.asset_key:
            raise ValueError("memorial upload identity is invalid")
        if not 1 <= self.size_bytes <= MAX_UPLOAD_BYTES or _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("memorial upload metadata is invalid")
        _require_utc(self.expires_at, label="memorial upload expiry")


@dataclass(frozen=True, slots=True)
class MemorialUploadTicket:
    """Short-lived browser POST capability constrained by an S3 upload policy."""

    upload_url: str = field(repr=False)
    expires_at: datetime
    fields: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.upload_url.startswith("https://") or len(self.upload_url) > 4096:
            raise ValueError("memorial upload URL is invalid")
        _require_utc(self.expires_at, label="memorial upload ticket expiry")
        names = frozenset(self.fields)
        if not _POST_REQUIRED_FIELDS.issubset(names) or not names.issubset(
            _POST_REQUIRED_FIELDS | _POST_OPTIONAL_FIELDS
        ):
            raise ValueError("memorial upload fields are invalid")
        if any(not isinstance(value, str) or not value for value in self.fields.values()):
            raise ValueError("memorial upload fields are invalid")
        try:
            decoded = base64.b64decode(
                self.fields["x-amz-checksum-sha256"],
                validate=True,
            )
        except ValueError, binascii.Error:
            raise ValueError("memorial upload checksum is invalid") from None
        if len(decoded) != hashlib.sha256().digest_size:
            raise ValueError("memorial upload checksum is invalid")
        if self.fields["x-amz-algorithm"] != "AWS4-HMAC-SHA256":
            raise ValueError("memorial upload algorithm is invalid")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


@dataclass(frozen=True, slots=True)
class MemorialMemory:
    """One durable generated memory; its content and object identity stay out of repr."""

    cycle: int
    participant: ParticipantSlot
    unlocked_at: datetime
    generated_at: datetime
    image_asset_key: str = field(repr=False)
    narrative: str = field(repr=False)
    width: Literal[1920] = MEMORIAL_IMAGE_WIDTH
    height: Literal[1080] = MEMORIAL_IMAGE_HEIGHT

    def __post_init__(self) -> None:
        if self.cycle < 1 or not self.image_asset_key or not self.narrative.strip():
            raise ValueError("memorial memory is incomplete")
        if len(self.narrative) > 2_000:
            raise ValueError("memorial narrative is too long")
        _require_utc(self.unlocked_at, label="memorial unlock timestamp")
        _require_utc(self.generated_at, label="memorial generation timestamp")
        if self.generated_at < self.unlocked_at:
            raise ValueError("memorial generation cannot precede its unlock")
        if self.width != MEMORIAL_IMAGE_WIDTH or self.height != MEMORIAL_IMAGE_HEIGHT:
            raise ValueError("memorial image dimensions are invalid")


@dataclass(frozen=True, slots=True)
class ResolvedMemorialMemory:
    memory: MemorialMemory
    image_url: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.image_url.startswith("https://") or len(self.image_url) > 4096:
            raise ValueError("memorial image URL is invalid")


@dataclass(frozen=True, slots=True)
class MemorialGenerationJob:
    """Private worker claim for one stable owner/cycle generation."""

    requester_key: str = field(repr=False)
    requester_display_name: str = field(repr=False)
    cycle: int
    participant: ParticipantSlot
    unlocked_at: datetime
    upload_asset_key: str = field(repr=False)
    result_asset_key: str = field(repr=False)
    generation_attempt: int = 0
    narrative: str | None = field(default=None, repr=False)
    image_asset_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _require_requester_key(self.requester_key)
        if (
            self.cycle < 1
            or not self.requester_display_name.strip()
            or not self.upload_asset_key
            or not self.result_asset_key
            or self.generation_attempt < 0
        ):
            raise ValueError("memorial generation job is invalid")
        _require_utc(self.unlocked_at, label="memorial unlock timestamp")
        if self.narrative is not None and (
            not self.narrative.strip() or len(self.narrative) > 2_000
        ):
            raise ValueError("memorial narrative checkpoint is invalid")
        if self.image_asset_key is not None and (
            self.image_asset_key != self.result_asset_key or self.narrative is None
        ):
            raise ValueError("memorial image checkpoint is invalid")


@dataclass(frozen=True, slots=True)
class GeneratedMemorialImage:
    """Validated image generator output before its private persistence boundary."""

    image_bytes: bytes = field(repr=False)
    image_content_type: Literal["image/png"] = "image/png"
    width: Literal[1920] = MEMORIAL_IMAGE_WIDTH
    height: Literal[1080] = MEMORIAL_IMAGE_HEIGHT

    def __post_init__(self) -> None:
        if not self.image_bytes:
            raise ValueError("generated memorial image is invalid")
        if self.width != MEMORIAL_IMAGE_WIDTH or self.height != MEMORIAL_IMAGE_HEIGHT:
            raise ValueError("generated memorial image dimensions are invalid")


class MemorialSessionStore(Protocol):
    def get_session(self, *, session_hash: str) -> SessionRecord | None: ...


class MemorialRepository(Protocol):
    """Own atomic owner/cycle, one-generation, idempotency, and reset invariants."""

    def get_snapshot(self, *, requester_key: str) -> MemorialSnapshot: ...

    def reserve_upload(
        self,
        *,
        requester_key: str,
        expected_cycle: int,
        content_type: MemorialUploadContentType,
        size_bytes: int,
        sha256: str,
        idempotency_hash: str,
        now: datetime,
    ) -> MemorialUploadReservation: ...

    def get_upload(
        self,
        *,
        requester_key: str,
        cycle: int,
    ) -> MemorialUploadReservation | None: ...

    def get_failed_generation(
        self,
        *,
        requester_key: str,
        cycle: int,
    ) -> MemorialGenerationJob | None: ...

    def queue_generation(
        self,
        *,
        requester_key: str,
        expected_cycle: int,
        idempotency_hash: str,
        now: datetime,
    ) -> MemorialSnapshot: ...

    def get_memory(self, *, requester_key: str, cycle: int) -> MemorialMemory | None: ...

    def reset_affection(
        self,
        *,
        requester_key: str,
        expected_cycle: int,
        reset_score: Literal[500],
        idempotency_hash: str,
        now: datetime,
    ) -> MemorialSnapshot: ...

    def claim_generation(
        self,
        *,
        requester_key: str,
        cycle: int,
        now: datetime,
    ) -> MemorialGenerationJob | None: ...

    def complete_generation(
        self,
        *,
        job: MemorialGenerationJob,
        generated_at: datetime,
    ) -> MemorialMemory: ...

    def checkpoint_narrative(
        self,
        *,
        job: MemorialGenerationJob,
        narrative: str,
        now: datetime,
    ) -> MemorialGenerationJob: ...

    def checkpoint_image(
        self,
        *,
        job: MemorialGenerationJob,
        image_asset_key: str,
        now: datetime,
    ) -> MemorialGenerationJob: ...

    def release_generation_to_queue(
        self,
        *,
        job: MemorialGenerationJob,
        released_at: datetime,
    ) -> None: ...

    def fail_generation(
        self,
        *,
        job: MemorialGenerationJob,
        failed_at: datetime,
        preserve_derived: bool,
    ) -> None: ...


class MemorialAssetStore(Protocol):
    def create_upload_ticket(
        self,
        reservation: MemorialUploadReservation,
    ) -> MemorialUploadTicket: ...

    def verify_upload(self, reservation: MemorialUploadReservation) -> bool: ...

    def load_upload(self, job: MemorialGenerationJob) -> bytes: ...

    def existing_generated(self, job: MemorialGenerationJob) -> str | None: ...

    def store_generated(
        self,
        *,
        job: MemorialGenerationJob,
        generated: GeneratedMemorialImage,
        now: datetime,
    ) -> str: ...

    def delete_upload(self, job: MemorialGenerationJob) -> None: ...

    def delete_reservation(self, reservation: MemorialUploadReservation) -> None: ...

    def memory_image_url(self, memory: MemorialMemory) -> str: ...


class MemorialJobQueue(Protocol):
    """Deliver only an opaque owner key and cycle to the generation worker."""

    def send(self, *, requester_key: str, cycle: int) -> None: ...


class RecentQuestionSource(Protocol):
    def latest_questions(self, *, requester_key: str, limit: int) -> tuple[str, ...]: ...


class MemorialContentGenerator(Protocol):
    def validate_image_inputs(
        self,
        *,
        participant: ParticipantSlot,
        source_image: bytes,
    ) -> None: ...

    def generate_narrative(
        self,
        *,
        participant: ParticipantSlot,
        requester_display_name: str,
        questions: tuple[str, ...],
        achieved_on: date,
    ) -> str: ...

    def generate_image(
        self,
        *,
        participant: ParticipantSlot,
        requester_display_name: str,
        questions: tuple[str, ...],
        source_image: bytes,
        narrative: str,
        achieved_on: date,
    ) -> GeneratedMemorialImage: ...


class MemorialAuthorizer:
    """Authenticate a Records member and bind every operation to that session owner."""

    def __init__(
        self,
        *,
        store: MemorialSessionStore,
        configuration: MemorialSecurityConfiguration,
    ) -> None:
        self._store = store
        self._configuration = configuration

    def authenticate(self, *, raw_session: str | None, now: datetime) -> SessionRecord:
        if not raw_session:
            raise MemorialFailure("AUTHENTICATION_REQUIRED", 401)
        stored = self._store.get_session(
            session_hash=session_hash(self._configuration.session_hmac_key, raw_session)
        )
        if stored is None or stored.expires_at <= int(_utc(now).timestamp()):
            raise MemorialFailure("AUTHENTICATION_REQUIRED", 401)
        try:
            _require_requester_key(stored.requester_key)
        except ValueError:
            raise MemorialFailure("MEMORIAL_STATE_INVALID", 503) from None
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
            raise MemorialFailure("ORIGIN_INVALID", 403)
        if not raw_csrf or not csrf_header or not hmac.compare_digest(raw_csrf, csrf_header):
            raise MemorialFailure("CSRF_INVALID", 403)
        expected_csrf = csrf_hash(self._configuration.session_hmac_key, raw_csrf)
        if not hmac.compare_digest(expected_csrf, session.csrf_hash):
            raise MemorialFailure("CSRF_INVALID", 403)
        if idempotency_key is None or _IDEMPOTENCY.fullmatch(idempotency_key) is None:
            raise MemorialFailure("IDEMPOTENCY_KEY_INVALID", 400)
        return hashlib.sha256(idempotency_key.encode()).hexdigest()


class MemorialService:
    """Coordinate owner-scoped HTTP operations without exposing private storage identities."""

    def __init__(
        self,
        *,
        repository: MemorialRepository,
        assets: MemorialAssetStore,
        queue: MemorialJobQueue,
    ) -> None:
        self._repository = repository
        self._assets = assets
        self._queue = queue

    def get_state(self, *, requester_key: str) -> MemorialSnapshot:
        return self._repository.get_snapshot(requester_key=requester_key)

    def prepare_upload(
        self,
        *,
        requester_key: str,
        expected_cycle: int,
        content_type: MemorialUploadContentType,
        size_bytes: int,
        sha256: str,
        idempotency_hash: str,
        now: datetime,
    ) -> tuple[int, MemorialUploadTicket]:
        snapshot = self._repository.get_snapshot(requester_key=requester_key)
        if snapshot.cycle != expected_cycle:
            raise MemorialFailure("MEMORIAL_STATE_CONFLICT", 409)
        if snapshot.state not in {"unlocked", "failed"}:
            raise MemorialFailure("MEMORIAL_UPLOAD_NOT_ALLOWED", 409)
        if snapshot.state == "failed":
            failed_job = self._repository.get_failed_generation(
                requester_key=requester_key,
                cycle=expected_cycle,
            )
            if failed_job is None:
                raise MemorialFailure("MEMORIAL_STATE_INVALID", 503)
            if (
                failed_job.image_asset_key is not None
                or self._assets.existing_generated(failed_job) is not None
            ):
                raise MemorialFailure("MEMORIAL_RECOVERY_REQUIRED", 409)
            if failed_job.generation_attempt >= 3:
                raise MemorialFailure("MEMORIAL_GENERATION_ATTEMPTS_EXHAUSTED", 409)
        reservation = self._repository.reserve_upload(
            requester_key=requester_key,
            expected_cycle=expected_cycle,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
            idempotency_hash=idempotency_hash,
            now=_utc(now),
        )
        ticket = self._assets.create_upload_ticket(reservation)
        expected_checksum = base64.b64encode(bytes.fromhex(reservation.sha256)).decode()
        if (
            ticket.fields.get("Content-Type") != reservation.content_type
            or ticket.fields.get("x-amz-checksum-sha256") != expected_checksum
            or ticket.expires_at != reservation.expires_at
        ):
            raise MemorialFailure("MEMORIAL_UPLOAD_INVALID", 503)
        return reservation.cycle, ticket

    def queue_generation(
        self,
        *,
        requester_key: str,
        expected_cycle: int,
        confirmation: str,
        idempotency_hash: str,
        now: datetime,
    ) -> MemorialSnapshot:
        if confirmation != GENERATE_CONFIRMATION:
            raise MemorialFailure("MEMORIAL_GENERATION_CONFIRMATION_REQUIRED", 400)
        snapshot = self._repository.get_snapshot(requester_key=requester_key)
        if snapshot.cycle != expected_cycle:
            raise MemorialFailure("MEMORIAL_STATE_CONFLICT", 409)
        if snapshot.state == "unlocked":
            upload = self._repository.get_upload(
                requester_key=requester_key,
                cycle=expected_cycle,
            )
            if (
                upload is None
                or upload.expires_at <= _utc(now)
                or not self._assets.verify_upload(upload)
            ):
                raise MemorialFailure("MEMORIAL_UPLOAD_REQUIRED", 409)
        elif snapshot.state == "failed":
            failed_job = self._repository.get_failed_generation(
                requester_key=requester_key,
                cycle=expected_cycle,
            )
            if failed_job is None:
                raise MemorialFailure("MEMORIAL_STATE_INVALID", 503)
            existing_result = self._assets.existing_generated(failed_job)
            if failed_job.image_asset_key is None and existing_result is None:
                if failed_job.generation_attempt >= 3:
                    raise MemorialFailure("MEMORIAL_GENERATION_ATTEMPTS_EXHAUSTED", 409)
                upload = self._repository.get_upload(
                    requester_key=requester_key,
                    cycle=expected_cycle,
                )
                if (
                    failed_job.narrative is None
                    or upload is None
                    or upload.expires_at <= _utc(now)
                    or not self._assets.verify_upload(upload)
                ):
                    raise MemorialFailure("MEMORIAL_UPLOAD_REQUIRED", 409)
        queued = self._repository.queue_generation(
            requester_key=requester_key,
            expected_cycle=expected_cycle,
            idempotency_hash=idempotency_hash,
            now=_utc(now),
        )
        try:
            self._queue.send(requester_key=requester_key, cycle=queued.cycle)
        except MemorialFailure:
            raise
        except Exception:
            raise MemorialFailure("MEMORIAL_QUEUE_UNAVAILABLE", 503) from None
        return queued

    def get_memory(self, *, requester_key: str, cycle: int) -> ResolvedMemorialMemory:
        if cycle < 1:
            raise MemorialFailure("REQUEST_INVALID", 400)
        memory = self._repository.get_memory(requester_key=requester_key, cycle=cycle)
        if memory is None:
            raise MemorialFailure("MEMORIAL_NOT_FOUND", 404)
        return ResolvedMemorialMemory(
            memory=memory,
            image_url=self._assets.memory_image_url(memory),
        )

    def reset(
        self,
        *,
        requester_key: str,
        expected_cycle: int,
        confirmation: str,
        idempotency_hash: str,
        now: datetime,
    ) -> MemorialSnapshot:
        if confirmation != RESET_CONFIRMATION:
            raise MemorialFailure("MEMORIAL_RESET_CONFIRMATION_REQUIRED", 400)
        snapshot = self._repository.get_snapshot(requester_key=requester_key)
        replay = snapshot.state == "locked" and snapshot.cycle == expected_cycle + 1
        current = snapshot.cycle == expected_cycle
        if not current and not replay:
            raise MemorialFailure("MEMORIAL_STATE_CONFLICT", 409)
        if current and snapshot.state in {"queued", "generating", "locked"}:
            raise MemorialFailure("MEMORIAL_RESET_NOT_ALLOWED", 409)
        if current and snapshot.state == "failed":
            failed_job = self._repository.get_failed_generation(
                requester_key=requester_key,
                cycle=expected_cycle,
            )
            if failed_job is None:
                raise MemorialFailure("MEMORIAL_STATE_INVALID", 503)
            if (
                failed_job.narrative is not None
                or failed_job.image_asset_key is not None
                or self._assets.existing_generated(failed_job) is not None
            ):
                raise MemorialFailure("MEMORIAL_RECOVERY_REQUIRED", 409)
        reservation = self._repository.get_upload(
            requester_key=requester_key,
            cycle=expected_cycle,
        )
        reset = self._repository.reset_affection(
            requester_key=requester_key,
            expected_cycle=expected_cycle,
            reset_score=AFFECTION_RESET_SCORE,
            idempotency_hash=idempotency_hash,
            now=_utc(now),
        )
        if reservation is not None:
            try:
                self._assets.delete_reservation(reservation)
            except MemorialFailure:
                raise
            except Exception:
                raise MemorialFailure("MEMORIAL_UPLOAD_CLEANUP_FAILED", 503) from None
        return reset


class MemorialGenerationService:
    """Run one resumable worker claim through questions, generation, and private assets."""

    def __init__(
        self,
        *,
        repository: MemorialRepository,
        assets: MemorialAssetStore,
        questions: RecentQuestionSource,
        generator: MemorialContentGenerator,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._assets = assets
        self._questions = questions
        self._generator = generator
        self._clock = clock or (lambda: datetime.now(UTC))

    def process(
        self,
        *,
        requester_key: str,
        cycle: int,
        receive_count: int,
        max_generation_attempts: int = 3,
        now: datetime,
        deadline: datetime,
    ) -> MemorialMemory | None:
        now = _utc(now)
        deadline = _utc(deadline)
        _require_requester_key(requester_key)
        if cycle < 1 or receive_count < 1 or max_generation_attempts < 1 or deadline <= now:
            raise MemorialFailure("MEMORIAL_JOB_INVALID", 400)
        job = self._repository.claim_generation(
            requester_key=requester_key,
            cycle=cycle,
            now=now,
        )
        if job is None:
            return None
        if job.generation_attempt < 1:
            raise MemorialFailure("MEMORIAL_JOB_INVALID", 503)
        try:
            recent: tuple[str, ...] | None = None
            source_image: bytes | None = None
            recovered_asset_key: str | None = None
            generated_asset_key: str | None = None
            if job.image_asset_key is not None:
                recovered_asset_key = self._assets.existing_generated(job)
                if recovered_asset_key != job.image_asset_key:
                    raise MemorialFailure("MEMORIAL_GENERATED_ASSET_MISSING", 503)
                generated_asset_key = recovered_asset_key
            else:
                recovered_asset_key = self._assets.existing_generated(job)
                if recovered_asset_key is None or job.narrative is None:
                    source_image = self._assets.load_upload(job)
                    self._generator.validate_image_inputs(
                        participant=job.participant,
                        source_image=source_image,
                    )
            if job.generation_attempt > max_generation_attempts and (
                job.narrative is None
                or (job.image_asset_key is None and recovered_asset_key is None)
            ):
                raise MemorialFailure("MEMORIAL_GENERATION_ATTEMPTS_EXHAUSTED", 503)
            if job.narrative is None:
                recent = self._recent_questions(job)
                self._require_budget(
                    deadline,
                    (
                        MEMORIAL_NARRATIVE_START_BUDGET
                        if recovered_asset_key is None and job.image_asset_key is None
                        else MEMORIAL_IMAGE_START_BUDGET
                    ),
                )
                narrative = self._generator.generate_narrative(
                    participant=job.participant,
                    requester_display_name=job.requester_display_name,
                    questions=recent,
                    achieved_on=achieved_date(job.unlocked_at),
                )
                if not narrative.strip() or len(narrative) > 2_000:
                    raise MemorialFailure("MEMORIAL_NARRATIVE_INVALID", 503)
                job = self._repository.checkpoint_narrative(
                    job=job,
                    narrative=narrative,
                    now=now,
                )
            if job.image_asset_key is None:
                if job.narrative is None:
                    raise MemorialFailure("MEMORIAL_CHECKPOINT_INVALID", 503)
                asset_key = recovered_asset_key
                if asset_key is None:
                    if recent is None:
                        recent = self._recent_questions(job)
                    if source_image is None:
                        raise MemorialFailure("MEMORIAL_CHECKPOINT_INVALID", 503)
                    self._require_budget(deadline, MEMORIAL_IMAGE_START_BUDGET)
                    generated = self._generator.generate_image(
                        participant=job.participant,
                        requester_display_name=job.requester_display_name,
                        questions=recent,
                        source_image=source_image,
                        narrative=job.narrative,
                        achieved_on=achieved_date(job.unlocked_at),
                    )
                    asset_key = self._assets.store_generated(
                        job=job,
                        generated=generated,
                        now=now,
                    )
                generated_asset_key = asset_key
                if asset_key != job.result_asset_key:
                    raise MemorialFailure("MEMORIAL_CHECKPOINT_INVALID", 503)
                job = self._repository.checkpoint_image(
                    job=job,
                    image_asset_key=asset_key,
                    now=now,
                )
            if job.narrative is None or job.image_asset_key is None:
                raise MemorialFailure("MEMORIAL_CHECKPOINT_INVALID", 503)
            self._assets.delete_upload(job)
            return self._repository.complete_generation(job=job, generated_at=now)
        except Exception:
            if job.generation_attempt < max_generation_attempts:
                self._repository.release_generation_to_queue(job=job, released_at=now)
            else:
                preserve_derived = generated_asset_key is not None
                if not preserve_derived and job.narrative is not None:
                    try:
                        preserve_derived = self._assets.existing_generated(job) is not None
                    except Exception:
                        self._repository.release_generation_to_queue(job=job, released_at=now)
                        raise
                try:
                    self._assets.delete_upload(job)
                finally:
                    self._repository.fail_generation(
                        job=job,
                        failed_at=now,
                        preserve_derived=preserve_derived,
                    )
            raise

    def _require_budget(self, deadline: datetime, required: timedelta) -> None:
        current = _utc(self._clock())
        if deadline - current < required:
            raise MemorialFailure("MEMORIAL_GENERATION_DEADLINE", 503)

    def _recent_questions(self, job: MemorialGenerationJob) -> tuple[str, ...]:
        recent = self._questions.latest_questions(
            requester_key=job.requester_key,
            limit=RECENT_QUESTION_LIMIT,
        )
        if (
            not recent
            or len(recent) > RECENT_QUESTION_LIMIT
            or any(not question.strip() for question in recent)
        ):
            raise MemorialFailure("MEMORIAL_QUESTIONS_INVALID", 503)
        return recent


def achieved_date(value: datetime) -> date:
    """Return the achievement date shown in the product's Asia/Tokyo time zone."""

    return _utc(value).astimezone(_TOKYO).date()


def _require_requester_key(value: str) -> None:
    if _OPAQUE_REQUESTER_KEY.fullmatch(value) is None:
        raise ValueError("requester identity is invalid")


def _require_utc(value: datetime, *, label: str) -> None:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{label} must be UTC")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MemorialFailure("REQUEST_INVALID", 400)
    return value.astimezone(UTC)
