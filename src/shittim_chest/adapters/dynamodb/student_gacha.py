"""Recruitment progress, Discord offers, and claim-fenced Core work in DynamoDB."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ParamSpec, TypeVar
from uuid import NAMESPACE_URL, uuid5

from botocore.exceptions import BotoCoreError, ClientError

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.deployment_lock import deployment_lock_open_check
from shittim_chest.adapters.dynamodb.serializer import DynamoItem
from shittim_chest.adapters.dynamodb.student_gacha_activity import (
    STUDENT_GACHA_ACTIVITY_KEY,
    STUDENT_GACHA_ACTIVITY_TYPE,
)
from shittim_chest.application.ports import RepositoryConflict, RepositoryUnavailable
from shittim_chest.application.student_gacha_models import (
    GachaPresentation,
    StoredGachaImage,
    Student,
    normalize_teacher_name,
)
from shittim_chest.application.student_gacha_state import GachaJob, GachaJobKind, GachaOffer
from shittim_chest.domain import ParticipantSlot

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

_GACHA_THRESHOLD = 5
_CLAIM_TIME = timedelta(minutes=6)
_RETRY_DELAY = timedelta(seconds=15)
_WARNING_INTERVAL = timedelta(minutes=1)
_JOB_QUERY_LIMIT = 16
_ACTIVITY_LIMIT = 100_000
P = ParamSpec("P")
T = TypeVar("T")


class DynamoDbStudentGachaRepository:
    """Keep one outstanding offer per owner and separate work from waiting offers."""

    def __init__(self, *, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def admission_actions(
        self,
        *,
        debate_id: str,
        owner_key: str,
        guild_id: str,
        channel_id: str,
        status_message_id: str,
        at: datetime,
    ) -> tuple[TransactWriteItemTypeDef, ...]:
        """Bind completion to the admission cycle or reannounce its pending offer."""

        owner = self._get(_owner_key(owner_key))
        if owner is None:
            generation = 0
            active_offer = None
        else:
            generation = _number(owner, "generation")
            active_offer = _optional_text(owner, "active_offer_id")
        owner_fence = self._owner_fence(owner_key, owner)
        if active_offer is None:
            return (
                owner_fence,
                self._put_new(
                    {
                        **_eligible_key(debate_id),
                        "owner_key": owner_key,
                        "generation": generation,
                        "status_message_id": status_message_id,
                    }
                ),
            )
        offer = self._require_offer(active_offer)
        if _text(offer, "state") != "open" or "notice_job_id" in offer:
            return (owner_fence, self._version_check(offer))
        job = GachaJob(
            job_id=_id("reminder", debate_id),
            kind=GachaJobKind.OFFER,
            guild_id=guild_id,
            channel_id=channel_id,
            after_message_id=status_message_id,
            offer_id=active_offer,
            created_at=at,
            next_available_at=at,
            previous_offer_channel_id=_optional_text(offer, "channel_id"),
            previous_offer_message_id=_optional_text(offer, "message_id"),
        )
        return (
            owner_fence,
            self._replace(offer, {**offer, "notice_job_id": job.job_id}),
            *self._enqueue_actions(job),
        )

    def completion_actions(
        self, *, debate_id: str, guild_id: str, channel_id: str, at: datetime
    ) -> tuple[TransactWriteItemTypeDef, ...]:
        """Advance progress only for a previously eligible successful debate."""

        eligible = self._get(_eligible_key(debate_id))
        if eligible is None:
            return ()
        owner_key = _text(eligible, "owner_key")
        owner = self._get(_owner_key(owner_key))
        generation = 0 if owner is None else _number(owner, "generation")
        if _number(eligible, "generation") != generation or (
            owner is not None and "active_offer_id" in owner
        ):
            return (self._delete(_eligible_key(debate_id)),)
        count = 0 if owner is None else _number(owner, "count")
        if count >= _GACHA_THRESHOLD:
            raise RepositoryConflict("gacha progress exceeds threshold")
        count += 1
        offer_id = _id("offer", debate_id) if count == _GACHA_THRESHOLD else None
        job = GachaJob(
            job_id=_id("progress", debate_id),
            kind=GachaJobKind.PROGRESS if offer_id is None else GachaJobKind.OFFER,
            guild_id=guild_id,
            channel_id=channel_id,
            after_message_id=_text(eligible, "status_message_id"),
            remaining=_GACHA_THRESHOLD - count if offer_id is None else None,
            offer_id=offer_id,
            created_at=at,
            next_available_at=at,
        )
        updated: DynamoItem = {
            **_owner_key(owner_key),
            "count": count if offer_id is None else 0,
            "generation": generation if offer_id is None else generation + 1,
        }
        offer_actions: tuple[TransactWriteItemTypeDef, ...] = ()
        if offer_id is not None:
            updated["active_offer_id"] = offer_id
            offer_actions = (
                self._put_new(
                    {
                        **_offer_key(offer_id),
                        "owner_key": owner_key,
                        "debate_id": debate_id,
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "state": "open",
                        "notice_job_id": job.job_id,
                        "version": 0,
                    }
                ),
            )
        return (
            self._delete(_eligible_key(debate_id)),
            self._put_new({**updated, "version": 0})
            if owner is None
            else self._replace(owner, updated),
            *offer_actions,
            *self._enqueue_actions(job),
        )

    async def recovery_state(self, offer_id: str) -> str:
        """Read only the public-safe lifecycle state for operator verification."""

        _validate_offer_id(offer_id)
        return await self._run(self._recovery_state, offer_id)

    async def resume_parked(self, offer_id: str, *, at: datetime) -> bool:
        """Requeue a parked job without selecting or generating a new result."""

        _validate_offer_id(offer_id)
        return await self._run(self._resume_parked, offer_id, at)

    async def get_offer(self, offer_id: str) -> GachaOffer | None:
        return await self._run(self._get_offer, offer_id)

    async def claim_warning(
        self, *, offer_id: str, guild_id: str, channel_id: str, message_id: str, at: datetime
    ) -> bool:
        return await self._run(self._claim_warning, offer_id, guild_id, channel_id, message_id, at)

    async def submit(
        self,
        *,
        offer_id: str,
        guild_id: str,
        channel_id: str,
        requester_id: str,
        message_id: str,
        interaction_id: str,
        teacher_name: str,
        at: datetime,
    ) -> bool:
        teacher = normalize_teacher_name(teacher_name)
        return await self._run(
            self._submit,
            offer_id,
            guild_id,
            channel_id,
            requester_id,
            message_id,
            interaction_id,
            teacher,
            at,
        )

    async def claim_next(self, *, claim_owner: str, at: datetime) -> GachaJob | None:
        return await self._run(self._claim_next, claim_owner, at)

    async def checkpoint(self, expected: GachaJob, updated: GachaJob, *, at: datetime) -> GachaJob:
        return await self._run(self._checkpoint, expected, updated, at)

    async def reschedule(self, expected: GachaJob, *, at: datetime) -> None:
        await self._run(self._reschedule, expected, at)

    async def finish(
        self,
        expected: GachaJob,
        *,
        at: datetime,
        result_message_id: str | None = None,
        reopen: bool = False,
        park: bool = False,
    ) -> None:
        await self._run(self._finish, expected, at, result_message_id, reopen, park)

    def _recovery_state(self, offer_id: str) -> str:
        item = self._get(_offer_key(offer_id))
        if item is None:
            return "missing"
        state = _text(item, "state")
        if state not in {"open", "claimed", "redeemed", "delivery_unconfirmed"}:
            raise RepositoryConflict("gacha offer state is invalid")
        return state

    def _resume_parked(self, offer_id: str, at: datetime) -> bool:
        offer = self._get(_offer_key(offer_id))
        if offer is None or offer.get("state") != "delivery_unconfirmed":
            return False
        owner = self._get(_owner_key(_text(offer, "owner_key")))
        if owner is None or owner.get("active_offer_id") != offer_id:
            raise RepositoryConflict("gacha active offer changed")
        stored = _stored(offer)
        paid_started = _bool(offer, "parked_paid_started")
        failure_code = _optional_text(offer, "failure_code")
        if failure_code is None and stored is None and not paid_started:
            failure_code = "gacha_recovery_without_name"
        job = GachaJob(
            job_id=_text(offer, "parked_job_id"),
            kind=GachaJobKind.DRAW,
            guild_id=_text(offer, "guild_id"),
            channel_id=_text(offer, "result_channel_id"),
            after_message_id=_text(offer, "parked_after_message_id"),
            created_at=_datetime(offer, "parked_job_created_at"),
            next_available_at=at,
            offer_id=offer_id,
            student=_student(offer),
            performer=_performer(offer),
            presentation=_presentation(offer),
            paid_started=paid_started,
            stored=stored,
            loading_message_id=_optional_text(offer, "loading_message_id"),
            result_message_id=_optional_text(offer, "result_message_id"),
            result_after_message_id=_optional_text(offer, "result_after_message_id"),
            failure_code=failure_code,
            version=_number(offer, "parked_job_version") + 1,
        )
        updated: DynamoItem = {**offer, "state": "claimed", "draw_job_id": job.job_id}
        for key in (
            "parked_job_id",
            "parked_at",
            "parked_job_version",
            "parked_job_created_at",
            "parked_after_message_id",
            "parked_paid_started",
            "loading_message_id",
            "result_message_id",
            "failure_code",
        ):
            updated.pop(key, None)
        try:
            self._transact(
                self._version_check(owner),
                self._replace(offer, updated),
                *self._enqueue_actions(job),
            )
        except RepositoryConflict:
            if self._recovery_state(offer_id) != "delivery_unconfirmed":
                return False
            raise
        return True

    def _get_offer(self, offer_id: str) -> GachaOffer | None:
        item = self._get(_offer_key(offer_id))
        if item is None:
            return None
        return GachaOffer(
            offer_id=offer_id,
            guild_id=_text(item, "guild_id"),
            channel_id=_text(item, "channel_id"),
            message_id=_optional_text(item, "message_id"),
            requester_id=self._offer_requester(item),
            live=_text(item, "state") == "open",
        )

    def _offer_requester(self, offer: DynamoItem) -> str:
        meta = self._get({"PK": f"DEBATE#{_text(offer, 'debate_id')}", "SK": "META"})
        if meta is None or meta.get("record_type") != "debate_meta":
            raise RepositoryConflict("gacha offer has no debate owner")
        return _text(meta, "requester_id")

    def _claim_warning(
        self, offer_id: str, guild_id: str, channel_id: str, message_id: str, at: datetime
    ) -> bool:
        offer = self._get(_offer_key(offer_id))
        if (
            offer is None
            or offer.get("guild_id") != guild_id
            or offer.get("channel_id") != channel_id
            or offer.get("message_id") != message_id
            or offer.get("state") != "open"
        ):
            return False
        previous = _optional_datetime(offer, "last_warning_at")
        if previous is not None and at < previous + _WARNING_INTERVAL:
            return False
        try:
            self._transact(self._replace(offer, {**offer, "last_warning_at": _timestamp(at)}))
        except RepositoryConflict:
            return False
        return True

    def _submit(
        self,
        offer_id: str,
        guild_id: str,
        channel_id: str,
        requester_id: str,
        message_id: str,
        interaction_id: str,
        teacher_name: str,
        at: datetime,
    ) -> bool:
        offer = self._get(_offer_key(offer_id))
        if (
            offer is None
            or offer.get("guild_id") != guild_id
            or self._offer_requester(offer) != requester_id
        ):
            return False
        job_id = _id("draw", interaction_id)
        receipt: DynamoItem = {
            "PK": f"GACHA#JOB#{job_id}",
            "SK": "SUBMIT",
            "offer_id": offer_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "message_id": message_id,
        }
        if self._get({"PK": receipt["PK"], "SK": "SUBMIT"}) == receipt:
            return True
        if (
            offer.get("channel_id") != channel_id
            or offer.get("message_id") != message_id
            or offer.get("state") != "open"
        ):
            return False
        job = GachaJob(
            job_id=job_id,
            kind=GachaJobKind.DRAW,
            guild_id=guild_id,
            channel_id=channel_id,
            after_message_id=message_id,
            offer_id=offer_id,
            teacher_name=teacher_name,
            student=_student(offer),
            performer=_performer(offer),
            presentation=_presentation(offer),
            paid_started="stored" in offer,
            stored=_stored(offer),
            result_after_message_id=(
                _optional_text(offer, "result_after_message_id")
                if channel_id == offer.get("result_channel_id")
                else message_id
            ),
            created_at=at,
            next_available_at=at,
        )
        try:
            self._transact(
                self._replace(offer, {**offer, "state": "claimed", "draw_job_id": job_id}),
                self._put_new(receipt),
                *self._enqueue_actions(job),
            )
        except RepositoryConflict:
            return self._get({"PK": receipt["PK"], "SK": "SUBMIT"}) == receipt
        return True

    def _claim_next(self, claim_owner: str, at: datetime) -> GachaJob | None:
        response = self._client.query(
            TableName=self._table_name,
            ConsistentRead=True,
            KeyConditionExpression="PK=:pk AND SK BETWEEN :first AND :last",
            ExpressionAttributeValues=marshal_item(
                {
                    ":pk": STUDENT_GACHA_ACTIVITY_KEY["PK"],
                    ":first": "JOB#",
                    ":last": f"JOB#{_timestamp(at)}#~",
                }
            ),
            Limit=_JOB_QUERY_LIMIT,
        )
        for item in response.get("Items", ()):
            pointer = unmarshal_item(item)
            job = self._get_job(_text(pointer, "job_id"))
            if job is None or _job_pointer_key(job)["SK"] != pointer.get("SK"):
                continue
            if job.next_available_at > at:
                continue
            claimed = replace(
                job,
                attempts=job.attempts + 1,
                claim_owner=claim_owner,
                claim_expires_at=at + _CLAIM_TIME,
                next_available_at=at + _CLAIM_TIME,
                version=job.version + 1,
            )
            try:
                self._transact(*self._move_job(job, claimed))
            except RepositoryConflict:
                continue
            return claimed
        return None

    def _checkpoint(self, expected: GachaJob, updated: GachaJob, at: datetime) -> GachaJob:
        _require_lease(expected, at)
        if (
            replace(
                updated,
                student=expected.student,
                performer=expected.performer,
                presentation=expected.presentation,
                teacher_name=expected.teacher_name,
                paid_started=expected.paid_started,
                stored=expected.stored,
                loading_message_id=expected.loading_message_id,
                result_message_id=expected.result_message_id,
                offer_message_id=expected.offer_message_id,
                failure_code=expected.failure_code,
            )
            != expected
        ):
            raise ValueError("gacha checkpoint cannot change job identity or lease")
        if (
            (expected.teacher_name != updated.teacher_name and updated.teacher_name is not None)
            or (expected.student is not None and updated.student != expected.student)
            or (expected.performer is not None and updated.performer != expected.performer)
            or (expected.presentation is not None and updated.presentation != expected.presentation)
            or (expected.paid_started and not updated.paid_started)
            or (expected.stored is not None and updated.stored != expected.stored)
            or (
                expected.result_message_id is not None
                and updated.result_message_id != expected.result_message_id
            )
            or (
                expected.loading_message_id is not None
                and updated.loading_message_id != expected.loading_message_id
            )
            or (
                expected.offer_message_id is not None
                and updated.offer_message_id != expected.offer_message_id
            )
        ):
            raise ValueError("gacha checkpoint cannot replace a saved selection or receipt")
        actions: list[TransactWriteItemTypeDef] = []
        if (
            expected.kind is GachaJobKind.OFFER
            and expected.offer_message_id is None
            and updated.offer_message_id is not None
        ):
            offer = self._require_offer(_require_id(expected.offer_id))
            if offer.get("notice_job_id") != expected.job_id:
                raise RepositoryConflict("gacha notice job changed")
            if offer.get("state") == "open":
                actions.append(
                    self._replace(
                        offer,
                        {
                            **offer,
                            "channel_id": expected.channel_id,
                            "message_id": updated.offer_message_id,
                        },
                    )
                )
            else:
                updated = replace(updated, offer_superseded=True)
        updated = replace(updated, version=expected.version + 1)
        actions.append(self._replace(_serialize_job(expected), _serialize_job(updated)))
        self._transact(*actions)
        return updated

    def _reschedule(self, expected: GachaJob, at: datetime) -> None:
        _require_lease(expected, at)
        pending = replace(
            expected,
            claim_owner=None,
            claim_expires_at=None,
            version=expected.version + 1,
            next_available_at=at + _RETRY_DELAY,
        )
        self._transact(*self._move_job(expected, pending))

    def _finish(
        self,
        expected: GachaJob,
        at: datetime,
        result_message_id: str | None,
        reopen: bool,
        park: bool,
    ) -> None:
        _require_lease(expected, at)
        if (reopen or park) and (expected.kind is not GachaJobKind.DRAW or (reopen and park)):
            raise ValueError("only draws may be reopened or parked")
        actions: list[TransactWriteItemTypeDef] = [
            self._delete(_job_key(expected.job_id), version=expected.version),
            self._delete(_job_pointer_key(expected)),
            self._activity_action(-1),
        ]
        if expected.kind is GachaJobKind.OFFER:
            offer = self._require_offer(_require_id(expected.offer_id))
            if offer.get("notice_job_id") != expected.job_id:
                raise RepositoryConflict("gacha notice job changed")
            updated = {**offer}
            del updated["notice_job_id"]
            actions.append(self._replace(offer, updated))
        if expected.kind is GachaJobKind.DRAW:
            offer_id = _require_id(expected.offer_id)
            offer = self._require_offer(offer_id)
            if offer.get("state") != "claimed" or offer.get("draw_job_id") != expected.job_id:
                raise RepositoryConflict("gacha draw job changed")
            updated = {**offer}
            del updated["draw_job_id"]
            updated.update(_serialize_selection(expected))
            if reopen or park:
                updated["state"] = "delivery_unconfirmed" if park else "open"
                updated["result_channel_id"] = expected.channel_id
                updated["result_after_message_id"] = (
                    expected.result_after_message_id or expected.after_message_id
                )
                if park:
                    updated["parked_job_id"] = expected.job_id
                    updated["parked_at"] = _timestamp(at)
                    updated["parked_job_version"] = expected.version
                    updated["parked_job_created_at"] = _timestamp(expected.created_at)
                    updated["parked_after_message_id"] = expected.after_message_id
                    updated["parked_paid_started"] = expected.paid_started
                    if expected.loading_message_id is not None:
                        updated["loading_message_id"] = expected.loading_message_id
                    if expected.result_message_id is not None:
                        updated["result_message_id"] = expected.result_message_id
                    if expected.failure_code is not None:
                        updated["failure_code"] = expected.failure_code
            else:
                message_id = expected.result_message_id
                if message_id is None or (
                    result_message_id is not None and message_id != result_message_id
                ):
                    raise ValueError("gacha finish requires a verified result receipt")
                updated["state"] = "redeemed"
                updated["result_message_id"] = message_id
                updated["result_channel_id"] = expected.channel_id
                actions.append(self._gallery_result(expected, offer_id=offer_id, at=at))
                owner = self._get(_owner_key(_text(offer, "owner_key")))
                if owner is None or owner.get("active_offer_id") != offer_id:
                    raise RepositoryConflict("gacha active offer changed")
                owner_updated = {**owner}
                del owner_updated["active_offer_id"]
                actions.append(self._replace(owner, owner_updated))
            actions.append(self._replace(offer, updated))
        self._transact(*actions)

    def _gallery_result(
        self, job: GachaJob, *, offer_id: str, at: datetime
    ) -> TransactWriteItemTypeDef:
        if job.student is None or job.performer is None or job.stored is None:
            raise ValueError("gacha gallery requires the saved reveal")
        posted_at = _timestamp(at)
        selection = _serialize_selection(job)
        return self._put_new(
            {
                "PK": f"GACHA#GUILD#{job.guild_id}",
                "SK": f"RESULT#{posted_at}#{offer_id}",
                "offer_id": offer_id,
                "posted_at": posted_at,
                "performer": selection["performer"],
                "student": selection["student"],
                "stored": selection["stored"],
            }
        )

    def _require_offer(self, offer_id: str) -> DynamoItem:
        offer = self._get(_offer_key(offer_id))
        if offer is None:
            raise RepositoryConflict("gacha offer is missing")
        return offer

    def _get_job(self, job_id: str) -> GachaJob | None:
        item = self._get(_job_key(job_id))
        return None if item is None else _deserialize_job(job_id, item)

    def _owner_fence(self, owner_key: str, owner: DynamoItem | None) -> TransactWriteItemTypeDef:
        if owner is None:
            return {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_owner_key(owner_key)),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            }
        return {
            "ConditionCheck": {
                "TableName": self._table_name,
                "Key": marshal_item(_owner_key(owner_key)),
                "ConditionExpression": "version=:version",
                "ExpressionAttributeValues": marshal_item({":version": _number(owner, "version")}),
            }
        }

    def _version_check(self, item: DynamoItem) -> TransactWriteItemTypeDef:
        return {
            "ConditionCheck": {
                "TableName": self._table_name,
                "Key": marshal_item({"PK": _text(item, "PK"), "SK": _text(item, "SK")}),
                "ConditionExpression": "version=:version",
                "ExpressionAttributeValues": marshal_item({":version": _number(item, "version")}),
            }
        }

    def _enqueue_actions(self, job: GachaJob) -> tuple[TransactWriteItemTypeDef, ...]:
        return (
            self._put_new(_serialize_job(job)),
            self._put_new({**_job_pointer_key(job), "job_id": job.job_id}),
            self._activity_action(1),
        )

    def _move_job(
        self, expected: GachaJob, updated: GachaJob
    ) -> tuple[TransactWriteItemTypeDef, ...]:
        change = self._replace(_serialize_job(expected), _serialize_job(updated))
        before, after = _job_pointer_key(expected), _job_pointer_key(updated)
        if before == after:
            return (change,)
        return (change, self._delete(before), self._put_new({**after, "job_id": updated.job_id}))

    def _activity_action(self, delta: int) -> TransactWriteItemTypeDef:
        condition = "record_type=:type AND #count > :zero AND #count <= :limit"
        if delta > 0:
            condition = (
                "attribute_not_exists(PK) OR "
                "(record_type=:type AND #count >= :zero AND #count < :limit)"
            )
        return {
            "Update": {
                "TableName": self._table_name,
                "Key": marshal_item(STUDENT_GACHA_ACTIVITY_KEY),
                "ConditionExpression": condition,
                "UpdateExpression": (
                    "SET record_type=:type, #count=if_not_exists(#count, :zero)+:delta"
                ),
                "ExpressionAttributeNames": {"#count": "count"},
                "ExpressionAttributeValues": marshal_item(
                    {
                        ":type": STUDENT_GACHA_ACTIVITY_TYPE,
                        ":zero": 0,
                        ":limit": _ACTIVITY_LIMIT,
                        ":delta": delta,
                    }
                ),
            }
        }

    def _get(self, key: DynamoItem) -> DynamoItem | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(key),
            ConsistentRead=True,
        )
        item = response.get("Item")
        return None if item is None else unmarshal_item(item)

    def _put_new(self, item: DynamoItem) -> TransactWriteItemTypeDef:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": marshal_item(item),
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }

    def _replace(self, expected: DynamoItem, updated: DynamoItem) -> TransactWriteItemTypeDef:
        version = _number(expected, "version")
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": marshal_item({**updated, "version": version + 1}),
                "ConditionExpression": "version=:version",
                "ExpressionAttributeValues": marshal_item({":version": version}),
            }
        }

    def _delete(self, key: DynamoItem, *, version: int | None = None) -> TransactWriteItemTypeDef:
        if version is None:
            return {"Delete": {"TableName": self._table_name, "Key": marshal_item(key)}}
        return {
            "Delete": {
                "TableName": self._table_name,
                "Key": marshal_item(key),
                "ConditionExpression": "version=:version",
                "ExpressionAttributeValues": marshal_item({":version": version}),
            }
        }

    def _transact(self, *actions: TransactWriteItemTypeDef) -> None:
        # Bound every admission/claim to an open deployment lock and reuse the token
        # when the SDK retries this exact payload.
        transaction = [deployment_lock_open_check(table_name=self._table_name), *actions]
        token = hashlib.sha256(json.dumps(transaction, sort_keys=True).encode()).hexdigest()[:36]
        try:
            self._client.transact_write_items(TransactItems=transaction, ClientRequestToken=token)
        except self._client.exceptions.TransactionCanceledException as error:
            reasons = error.response.get("CancellationReasons", [])
            if reasons and all(
                reason.get("Code") in {"None", "ConditionalCheckFailed", "TransactionConflict"}
                for reason in reasons
            ):
                raise RepositoryConflict("gacha transaction conflict") from None
            raise RepositoryUnavailable from None

    async def _run(self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except BotoCoreError, ClientError:
            raise RepositoryUnavailable from None


def _validate_offer_id(offer_id: str) -> None:
    if re.fullmatch(r"[0-9a-f]{32}", offer_id) is None:
        raise ValueError("gacha offer ID is invalid")


def _id(kind: str, key: str) -> str:
    return uuid5(NAMESPACE_URL, f"shittim-chest:student-gacha:{kind}:{key}").hex


def _require_id(value: str | None) -> str:
    if not value:
        raise RepositoryConflict("gacha offer identity is missing")
    return value


def _owner_key(owner_key: str) -> DynamoItem:
    return {"PK": f"GACHA#OWNER#{owner_key}", "SK": "STATE"}


def _eligible_key(debate_id: str) -> DynamoItem:
    return {"PK": f"GACHA#DEBATE#{debate_id}", "SK": "ELIGIBILITY"}


def _offer_key(offer_id: str) -> DynamoItem:
    return {"PK": f"GACHA#OFFER#{offer_id}", "SK": "META"}


def _job_key(job_id: str) -> DynamoItem:
    return {"PK": f"GACHA#JOB#{job_id}", "SK": "META"}


def _job_pointer_key(job: GachaJob) -> DynamoItem:
    return {
        "PK": STUDENT_GACHA_ACTIVITY_KEY["PK"],
        "SK": f"JOB#{_timestamp(job.next_available_at)}#{job.job_id}",
    }


def _serialize_selection(job: GachaJob) -> DynamoItem:
    item: DynamoItem = {}
    if job.student is not None:
        item["student"] = {"name": job.student.name, "school": job.student.school}
    if job.performer is not None:
        item["performer"] = job.performer.value
    if job.presentation is not None:
        item["presentation"] = {
            "appearance": job.presentation.appearance,
            "club": job.presentation.club,
            "role": job.presentation.role,
            "weapon": job.presentation.weapon,
            "greeting": list(job.presentation.greeting),
        }
    if job.stored is not None:
        item["stored"] = {
            "key": job.stored.key,
            "version_id": job.stored.version_id,
            "sha256": job.stored.sha256,
        }
    return item


def _serialize_job(job: GachaJob) -> DynamoItem:
    item: DynamoItem = {
        **_job_key(job.job_id),
        "kind": job.kind.value,
        "guild_id": job.guild_id,
        "channel_id": job.channel_id,
        "after_message_id": job.after_message_id,
        "created_at": _timestamp(job.created_at),
        "next_available_at": _timestamp(job.next_available_at),
        "paid_started": job.paid_started,
        "offer_superseded": job.offer_superseded,
        "attempts": job.attempts,
        "version": job.version,
        **_serialize_selection(job),
    }
    optional: DynamoItem = {
        "remaining": job.remaining,
        "offer_id": job.offer_id,
        "teacher_name": job.teacher_name,
        "loading_message_id": job.loading_message_id,
        "result_message_id": job.result_message_id,
        "result_after_message_id": job.result_after_message_id,
        "offer_message_id": job.offer_message_id,
        "previous_offer_channel_id": job.previous_offer_channel_id,
        "previous_offer_message_id": job.previous_offer_message_id,
        "failure_code": job.failure_code,
        "claim_owner": job.claim_owner,
        "claim_expires_at": (
            None if job.claim_expires_at is None else _timestamp(job.claim_expires_at)
        ),
    }
    item.update({key: value for key, value in optional.items() if value is not None})
    return item


def _deserialize_job(job_id: str, item: DynamoItem) -> GachaJob:
    return GachaJob(
        job_id=job_id,
        kind=GachaJobKind(_text(item, "kind")),
        guild_id=_text(item, "guild_id"),
        channel_id=_text(item, "channel_id"),
        after_message_id=_text(item, "after_message_id"),
        created_at=_datetime(item, "created_at"),
        next_available_at=_datetime(item, "next_available_at"),
        remaining=None if "remaining" not in item else _number(item, "remaining"),
        offer_id=_optional_text(item, "offer_id"),
        teacher_name=_optional_text(item, "teacher_name"),
        student=_student(item),
        performer=_performer(item),
        presentation=_presentation(item),
        paid_started=_bool(item, "paid_started"),
        stored=_stored(item),
        loading_message_id=_optional_text(item, "loading_message_id"),
        result_message_id=_optional_text(item, "result_message_id"),
        result_after_message_id=_optional_text(item, "result_after_message_id"),
        offer_message_id=_optional_text(item, "offer_message_id"),
        previous_offer_channel_id=_optional_text(item, "previous_offer_channel_id"),
        previous_offer_message_id=_optional_text(item, "previous_offer_message_id"),
        offer_superseded=_bool(item, "offer_superseded"),
        failure_code=_optional_text(item, "failure_code"),
        attempts=_number(item, "attempts"),
        version=_number(item, "version"),
        claim_owner=_optional_text(item, "claim_owner"),
        claim_expires_at=_optional_datetime(item, "claim_expires_at"),
    )


def _student(item: DynamoItem) -> Student | None:
    source = _optional_map(item, "student")
    return None if source is None else Student(_text(source, "name"), _text(source, "school"))


def _performer(item: DynamoItem) -> ParticipantSlot | None:
    value = _optional_text(item, "performer")
    return None if value is None else ParticipantSlot(value)


def _presentation(item: DynamoItem) -> GachaPresentation | None:
    source = _optional_map(item, "presentation")
    if source is None:
        return None
    student = _student(item)
    performer = _performer(item)
    greeting = source.get("greeting")
    if student is None or performer is None or not isinstance(greeting, list) or len(greeting) != 3:
        raise RepositoryConflict("gacha presentation is invalid")
    first, second, third = greeting
    if not isinstance(first, str) or not isinstance(second, str) or not isinstance(third, str):
        raise RepositoryConflict("gacha greeting is invalid")
    return GachaPresentation(
        student=student,
        performer=performer,
        appearance=_text(source, "appearance"),
        club=_text(source, "club"),
        role=_text(source, "role"),
        weapon=_text(source, "weapon"),
        greeting=(first, second, third),
    )


def _stored(item: DynamoItem) -> StoredGachaImage | None:
    source = _optional_map(item, "stored")
    if source is None:
        return None
    return StoredGachaImage(
        key=_text(source, "key"),
        version_id=_text(source, "version_id"),
        sha256=_text(source, "sha256"),
    )


def _optional_map(item: DynamoItem, name: str) -> DynamoItem | None:
    value = item.get(name)
    if value is None or isinstance(value, dict):
        return value
    raise RepositoryConflict("gacha record contains an invalid map")


def _bool(item: DynamoItem, name: str) -> bool:
    value = item.get(name)
    if not isinstance(value, bool):
        raise RepositoryConflict("gacha record contains an invalid boolean")
    return value


def _number(item: DynamoItem, name: str) -> int:
    value = item.get(name)
    if type(value) is not int or value < 0:
        raise RepositoryConflict("gacha record contains an invalid number")
    return value


def _text(item: DynamoItem, name: str) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value:
        raise RepositoryConflict("gacha record contains invalid text")
    return value


def _optional_text(item: DynamoItem, name: str) -> str | None:
    return None if name not in item else _text(item, name)


def _timestamp(at: datetime) -> str:
    if at.tzinfo is None or at.utcoffset() != UTC.utcoffset(at):
        raise ValueError("gacha time must be UTC")
    return at.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _datetime(item: DynamoItem, name: str) -> datetime:
    value = datetime.fromisoformat(_text(item, name))
    _timestamp(value)
    return value


def _optional_datetime(item: DynamoItem, name: str) -> datetime | None:
    return None if name not in item else _datetime(item, name)


def _require_lease(job: GachaJob, at: datetime) -> None:
    if job.claim_owner is None or job.claim_expires_at is None or job.claim_expires_at <= at:
        raise RepositoryConflict("gacha claim has expired")
