"""Process one durable student-gacha publication or draw at a time."""

from __future__ import annotations

import asyncio
import logging
import secrets
from bisect import bisect_right
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from shittim_chest.application.ports import RepositoryConflict, RepositoryUnavailable
from shittim_chest.application.student_gacha_discord import GachaDeliveryFailure
from shittim_chest.application.student_gacha_models import GachaGenerationFailure
from shittim_chest.domain import PARTICIPANTS

if TYPE_CHECKING:
    from shittim_chest.application.ports import Clock
    from shittim_chest.application.student_gacha_models import (
        GachaPresentation,
        SchoolBranding,
        StoredGachaImage,
        Student,
    )
    from shittim_chest.application.student_gacha_state import GachaJob, GachaOffer
    from shittim_chest.domain import ParticipantSlot

_LOGGER = logging.getLogger("shittim_chest")
_JOB_TIMEOUT_SECONDS = 330
_IDLE_POLL_SECONDS = 5
_NOTICE_ATTEMPTS = 8
_DRAW_ATTEMPTS = 12
# Upper roll boundaries for rarity 0 through 3. Rolls 98 and 99 select rarity 4.
_RARITY_THRESHOLDS = (5, 60, 90, 98)


class _Store(Protocol):
    async def get_offer(self, offer_id: str) -> GachaOffer | None: ...
    async def claim_next(self, *, claim_owner: str, at: datetime) -> GachaJob | None: ...
    async def checkpoint(
        self, expected: GachaJob, updated: GachaJob, *, at: datetime
    ) -> GachaJob: ...
    async def reschedule(self, expected: GachaJob, *, at: datetime) -> None: ...
    async def finish(
        self,
        expected: GachaJob,
        *,
        at: datetime,
        result_message_id: str | None = None,
        reopen: bool = False,
        park: bool = False,
    ) -> None: ...


class _Media(Protocol):
    async def roster(self) -> tuple[Student, ...]: ...
    async def loading_gif(self) -> bytes: ...
    async def references(self, performer: ParticipantSlot) -> tuple[bytes, bytes]: ...
    async def costume_reference(self, student: Student) -> bytes: ...
    async def school_branding(self, school: str) -> SchoolBranding: ...
    async def save(self, draw_id: str, png: bytes) -> StoredGachaImage: ...
    async def load(self, stored: StoredGachaImage) -> bytes: ...
    async def find(self, draw_id: str) -> StoredGachaImage | None: ...


class _Generator(Protocol):
    async def prepare(
        self, *, student: Student, performer: ParticipantSlot, costume_reference: bytes
    ) -> GachaPresentation: ...
    async def render(
        self,
        presentation: GachaPresentation,
        *,
        teacher_name: str,
        references: tuple[bytes, bytes],
        costume_reference: bytes,
        school_branding: SchoolBranding,
    ) -> bytes: ...


class _Discord(Protocol):
    async def send_progress(
        self,
        *,
        guild_id: str,
        channel_id: str,
        publication_id: str,
        after_message_id: str | None,
        remaining: int,
    ) -> str: ...
    async def send_offer(
        self,
        *,
        guild_id: str,
        channel_id: str,
        publication_id: str,
        after_message_id: str | None,
        offer_id: str,
    ) -> str: ...
    async def send_loading(
        self,
        *,
        guild_id: str,
        channel_id: str,
        publication_id: str,
        after_message_id: str | None,
        content: bytes,
    ) -> str: ...
    async def find_loading(
        self,
        *,
        guild_id: str,
        channel_id: str,
        publication_id: str,
        after_message_id: str | None,
        content: bytes,
    ) -> str | None: ...
    async def send_result(
        self,
        *,
        guild_id: str,
        channel_id: str,
        publication_id: str,
        after_message_id: str | None,
        content: bytes,
    ) -> str: ...
    async def send_failure(
        self,
        *,
        guild_id: str,
        channel_id: str,
        publication_id: str,
        after_message_id: str | None,
        retryable: bool,
    ) -> str: ...
    async def disable_offer(self, *, guild_id: str, channel_id: str, message_id: str) -> None: ...
    async def delete_loading(self, *, guild_id: str, channel_id: str, message_id: str) -> None: ...


class StudentGachaWorker:
    def __init__(
        self,
        *,
        store: _Store,
        media: _Media,
        generator: _Generator,
        discord: _Discord,
        clock: Clock,
        claim_owner: str,
    ) -> None:
        self._store = store
        self._media = media
        self._generator = generator
        self._discord = discord
        self._clock = clock
        self._claim_owner = claim_owner

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                if await self.process_next():
                    continue
            except RepositoryUnavailable, RepositoryConflict:
                _LOGGER.warning("student_gacha_repository_retry")
            try:
                async with asyncio.timeout(_IDLE_POLL_SECONDS):
                    await stop.wait()
            except TimeoutError:
                pass

    async def process_next(self) -> bool:
        job = await self._store.claim_next(claim_owner=self._claim_owner, at=self._clock.now())
        if job is None:
            return False
        try:
            async with asyncio.timeout(_JOB_TIMEOUT_SECONDS):
                if job.kind == "draw":
                    await self._draw(job)
                else:
                    await self._notice(job)
        except TimeoutError:
            # A paid-call checkpoint or a Discord receipt may already exist.
            # Let lease recovery reload the latest durable job.
            _LOGGER.warning("student_gacha_job_timeout", extra={"draw_job_id": job.job_id})
        return True

    async def _notice(self, job: GachaJob) -> None:
        try:
            if job.kind == "offer":
                job = await self._offer(job)
            else:
                await self._discord.send_progress(
                    guild_id=job.guild_id,
                    channel_id=job.channel_id,
                    publication_id=job.job_id,
                    after_message_id=job.after_message_id,
                    remaining=_required(job.remaining),
                )
        except GachaDeliveryFailure as error:
            _LOGGER.warning("student_gacha_notice_failed", extra={"draw_job_id": job.job_id})
            if error.retryable and job.attempts < _NOTICE_ATTEMPTS:
                # The offer checkpoint may have advanced the lease's revision.
                # That path will be recovered from the persisted receipt.
                await self._store.reschedule(job, at=self._clock.now())
                return
        await self._store.finish(job, at=self._clock.now())

    async def _offer(self, job: GachaJob) -> GachaJob:
        offer_id = _required(job.offer_id)
        if job.offer_message_id is None:
            offer = await self._store.get_offer(offer_id)
            if offer is None or not offer.live:
                return job
            message_id = await self._discord.send_offer(
                guild_id=job.guild_id,
                channel_id=job.channel_id,
                publication_id=job.job_id,
                after_message_id=job.after_message_id,
                offer_id=offer_id,
            )
            job = await self._checkpoint(job, offer_message_id=message_id)

        if job.offer_superseded:
            await self._disable(job.guild_id, job.channel_id, job.offer_message_id)
        elif job.previous_offer_channel_id is not None:
            await self._disable(
                job.guild_id, job.previous_offer_channel_id, job.previous_offer_message_id
            )
        return job

    async def _disable(self, guild_id: str, channel_id: str, message_id: str | None) -> None:
        if message_id is None:
            return
        try:
            await self._discord.disable_offer(
                guild_id=guild_id, channel_id=channel_id, message_id=message_id
            )
        except GachaDeliveryFailure:
            # The persisted offer binding already rejects the former button.
            _LOGGER.warning("student_gacha_disable_button_failed")

    async def _draw(self, job: GachaJob) -> None:
        if job.failure_code is None and job.stored is None:
            prepared = await self._prepare_draw(job)
            if prepared is None:
                return
            job = prepared
        try:
            if job.paid_started and job.stored is None and job.failure_code is None:
                await self._store.reschedule(job, at=self._clock.now())
            elif job.failure_code is not None:
                await self._settle_failure(job)
            else:
                await self._publish_result(job)
        except GachaDeliveryFailure as error:
            _LOGGER.warning("student_gacha_delivery_failed", extra={"draw_job_id": job.job_id})
            # A permanent failure with ambiguous history cannot safely be
            # reopened from another channel. Preserve the saved image and IDs.
            if not error.retryable:
                await self._park(job)
            else:
                await self._reschedule_draw(job, ambiguous=error.ambiguous)
        except GachaGenerationFailure:
            # Read the same immutable S3 object again after a transient failure.
            await self._reschedule_draw(job, ambiguous=True)

    async def _prepare_draw(self, job: GachaJob) -> GachaJob | None:
        if job.paid_started:
            try:
                stored = await self._media.find(_required(job.offer_id))
            except GachaGenerationFailure:
                await self._reschedule_draw(job, ambiguous=True)
                return None
            if stored is None:
                return await self._checkpoint(
                    job, failure_code="image_response_unknown", teacher_name=None
                )
            return await self._checkpoint(job, stored=stored, teacher_name=None)
        if job.loading_message_id is None:
            try:
                gif = await self._media.loading_gif()
                message_id = await self._discord.send_loading(
                    guild_id=job.guild_id,
                    channel_id=job.channel_id,
                    publication_id=job.job_id,
                    after_message_id=job.after_message_id,
                    content=gif,
                )
            except GachaGenerationFailure as error:
                return await self._checkpoint(job, failure_code=error.code, teacher_name=None)
            except GachaDeliveryFailure as error:
                _LOGGER.warning("student_gacha_loading_failed", extra={"draw_job_id": job.job_id})
                if not error.retryable or job.attempts >= _DRAW_ATTEMPTS:
                    if error.ambiguous:
                        job = await self._checkpoint(
                            job, failure_code="gacha_loading_unconfirmed", teacher_name=None
                        )
                    await self._park(job)
                else:
                    await self._store.reschedule(job, at=self._clock.now())
                return None
            job = await self._checkpoint(job, loading_message_id=message_id)
        return await self._generate(job)

    async def _reschedule_draw(self, job: GachaJob, *, ambiguous: bool) -> None:
        if job.attempts < _DRAW_ATTEMPTS:
            await self._store.reschedule(job, at=self._clock.now())
        elif ambiguous or job.loading_message_id is not None or job.stored is not None:
            await self._park(job)
        else:
            await self._store.finish(job, at=self._clock.now(), reopen=True)

    async def _park(self, job: GachaJob) -> None:
        await self._store.finish(job, at=self._clock.now(), park=True)
        _LOGGER.error("student_gacha_requires_recovery offer_id=%s", _required(job.offer_id))

    async def _generate(self, job: GachaJob) -> GachaJob:
        offer_id = _required(job.offer_id)
        try:
            if job.student is None:
                roster = await self._media.roster()
                job = await self._checkpoint(
                    job, student=_draw_student(roster), performer=secrets.choice(PARTICIPANTS)
                )
            performer = _required(job.performer)
            student = _required(job.student)
            school_branding = await self._media.school_branding(student.school)
            costume_reference = await self._media.costume_reference(student)
            if job.presentation is None:
                presentation = await self._generator.prepare(
                    student=student, performer=performer, costume_reference=costume_reference
                )
                job = await self._checkpoint(job, presentation=presentation)
            references = await self._media.references(performer)
            job = await self._checkpoint(job, paid_started=True)
            png = await self._generator.render(
                _required(job.presentation),
                teacher_name=_required(job.teacher_name),
                references=references,
                costume_reference=costume_reference,
                school_branding=school_branding,
            )
            stored = await self._media.save(offer_id, png)
            return await self._checkpoint(job, stored=stored, teacher_name=None)
        except GachaGenerationFailure as error:
            _LOGGER.warning(
                "student_gacha_generation_failed offer_id=%s reason_code=%s",
                offer_id,
                error.code,
            )
            if error.ambiguous and job.paid_started:
                # Recovery consults S3 before allowing an explicit retry.
                return job
            return await self._checkpoint(job, failure_code=error.code, teacher_name=None)

    async def _remove_loading(self, job: GachaJob) -> None:
        if job.loading_message_id is not None:
            await self._discord.delete_loading(
                guild_id=job.guild_id,
                channel_id=job.channel_id,
                message_id=job.loading_message_id,
            )

    async def _settle_failure(self, job: GachaJob) -> None:
        if job.failure_code == "gacha_loading_unconfirmed" and job.loading_message_id is None:
            message_id = await self._discord.find_loading(
                guild_id=job.guild_id,
                channel_id=job.channel_id,
                publication_id=job.job_id,
                after_message_id=job.after_message_id,
                content=await self._media.loading_gif(),
            )
            if message_id is not None:
                job = await self._checkpoint(job, loading_message_id=message_id)
        await self._remove_loading(job)
        try:
            await self._discord.send_failure(
                guild_id=job.guild_id,
                channel_id=job.channel_id,
                publication_id=job.job_id,
                after_message_id=job.after_message_id,
                retryable=True,
            )
        except GachaDeliveryFailure as error:
            if error.retryable and job.attempts < _NOTICE_ATTEMPTS:
                await self._store.reschedule(job, at=self._clock.now())
                return
        await self._store.finish(job, at=self._clock.now(), reopen=True)

    async def _publish_result(self, job: GachaJob) -> None:
        if job.result_message_id is None:
            content = await self._media.load(_required(job.stored))
            await self._remove_loading(job)
            message_id = await self._discord.send_result(
                guild_id=job.guild_id,
                channel_id=job.channel_id,
                publication_id=_required(job.offer_id),
                after_message_id=job.result_after_message_id or job.after_message_id,
                content=content,
            )
            job = await self._checkpoint(job, result_message_id=message_id)
        await self._store.finish(job, at=self._clock.now(), result_message_id=job.result_message_id)
        await self._disable(job.guild_id, job.channel_id, job.after_message_id)

    async def _checkpoint(self, job: GachaJob, **changes: object) -> GachaJob:
        return await self._store.checkpoint(job, replace(job, **changes), at=self._clock.now())


def _required[Value](value: Value | None) -> Value:
    if value is None:
        raise RepositoryConflict("student gacha job is incomplete")
    return value


def _draw_student(roster: tuple[Student, ...]) -> Student:
    rarity = bisect_right(_RARITY_THRESHOLDS, secrets.randbelow(100))
    candidates = tuple(student for student in roster if student.rarity == rarity)
    return secrets.choice(candidates)
