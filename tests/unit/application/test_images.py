"""Image import and delivery precede every provider call and survive retries."""

import asyncio
from dataclasses import replace
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError

from shittim_chest.application import (
    BindDiscordContextCommand,
    CancelDebateCommand,
    DebateSnapshot,
    OutboxRecoveryAbandoned,
    OutboxStatus,
    RetryDebateCommand,
)
from shittim_chest.application.models import (
    DeliveryAbandonReason,
    PendingDiscordImage,
    PhaseDeliveryPlan,
)
from shittim_chest.application.ports import DiscordOutboxRepository
from shittim_chest.domain import PARTICIPANTS, Candidate, CandidatePlan, DebatePhase, InitialOpinion
from shittim_chest.domain.attachments import DebateImage, ImageInput
from tests.unit.application.fakes import FakeOpenAI, FakeRepository
from tests.unit.application.test_service import (
    Dependencies,
    make_application,
    request,
)
from tests.unit.application.test_service import (
    dependencies as dependencies,
)

IMAGE_ID = "12345678-1234-5678-1234-abcdefabcdef"


def image(identity: str = IMAGE_ID) -> DebateImage:
    return DebateImage(
        identity,
        "image/png",
        123,
        "a" * 64,
        10,
        20,
        f"debate-images/{identity}/original.png",
        f"debate-images/{identity}/vision.webp",
    )


class ImageStore:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.imports = 0

    async def import_discord_attachment(self, pending: PendingDiscordImage) -> DebateImage:
        self.events.append("import")
        self.imports += 1
        if self.fail:
            raise ValueError("invalid image")
        return image()

    async def load_vision_images(self, images: tuple[DebateImage, ...]) -> tuple[ImageInput, ...]:
        return tuple(ImageInput(x.attachment_id, "image/webp", b"validated") for x in images)

    async def load_original(self, image: DebateImage) -> bytes:
        raise AssertionError("the fake drainer does not send binary files")


class ImageOpenAI:
    def __init__(self, original: FakeOpenAI, events: list[str]) -> None:
        self.original = original
        self.events = events
        self.calls: list[str] = []
        self.fail_decision = False

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.original, name)

        async def call(**kwargs: Any) -> Any:
            images = kwargs.pop("images", ())
            assert tuple(x.attachment_id for x in images) == (IMAGE_ID,)
            self.events.append("provider")
            self.calls.append(name)
            if name == "generate_decision" and self.fail_decision:
                raise ValueError("provider fixture failure")
            if name == "find_overlap_targets":
                return (PARTICIPANTS[0],)
            if name == "explore_alternatives":
                return (Candidate("image alternative", "own appeal", "acceptable cost"),)
            if name == "select_alternative":
                return CandidatePlan(kwargs["plan"].participant, kwargs["alternatives"])
            if name == "revise_initial_opinion":
                return InitialOpinion(
                    kwargs["plan"].participant, "revised", kwargs["plan"].selected.proposal
                )
            return await method(**kwargs)

        return call


class ImageOutbox:
    def __init__(self, repository: FakeRepository, events: list[str]) -> None:
        self.repository = repository
        self.events = events
        self.image_posts = 0

    async def get(self, *, debate_id: object, attempt_id: object, operation_id: str) -> Any:
        return self.repository.terminal_operations.get(operation_id)

    async def drain(self, *, expected: DebateSnapshot) -> None:
        plan = expected.terminal_delivery
        if plan is None:
            return
        for operation_id in plan.operation_ids:
            operation = self.repository.terminal_operations[operation_id]
            if operation.attachments and operation.status is not OutboxStatus.SENT:
                self.events.append("publish")
                self.image_posts += 1
            self.repository.terminal_operations[operation_id] = replace(
                operation,
                status=OutboxStatus.SENT,
                message_id="999",
                sent_at=operation.created_at,
                delivery_attempt=1,
            )

    async def terminate(self, *, expected: DebateSnapshot) -> bool:
        return True


@pytest.mark.asyncio
async def test_image_import_and_post_precede_all_model_calls_and_retry_reuses_receipt(
    dependencies: Dependencies,
) -> None:
    events: list[str] = []
    store = ImageStore(events)
    openai = ImageOpenAI(dependencies[5], events)
    openai.fail_decision = True
    values = list(dependencies)
    values[5] = cast(FakeOpenAI, openai)
    # The evidence fake also receives provider-ready images.
    evidence = dependencies[4]

    class ImageEvidence:
        async def prepare_evidence(
            self, *, question: str, images: tuple[ImageInput, ...] = ()
        ) -> Any:
            assert tuple(x.attachment_id for x in images) == (IMAGE_ID,)
            events.append("evidence")
            return await evidence.prepare_evidence(question=question)

    values[4] = cast(Any, ImageEvidence())
    outbox = ImageOutbox(dependencies[6], events)
    app = make_application(
        cast(Dependencies, tuple(values)),
        image_store=store,
        image_outbox=cast(DiscordOutboxRepository, outbox),
        outbox_recovery=cast(Any, outbox),
    )
    pending = PendingDiscordImage(
        IMAGE_ID,
        "123",
        "image/png",
        123,
        "https://cdn.discordapp.com/attachments/101/123/image.png?hm=secret",
    )
    accepted = await app.accept_debate(replace(request(), pending_attachments=(pending,)))
    await app.bind_discord_context(
        BindDiscordContextCommand(accepted.debate_id, "101", "102", "103")
    )
    await app.run_debate(accepted.debate_id)
    failed = dependencies[6].current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.pending_attachments == () and failed.attachments == (image(),)
    assert failed.attachment_message_ids == ((IMAGE_ID, "999"),)
    assert events.index("import") < events.index("publish") < events.index("provider")
    assert {
        "score_affection",
        "form_preferences",
        "select_candidates",
        "find_overlap_targets",
        "explore_alternatives",
        "select_alternative",
        "revise_initial_opinion",
        "generate_initial_opinion",
        "generate_final_proposal",
        "cast_vote",
        "generate_decision",
    } <= set(openai.calls)
    openai.fail_decision = False
    await app.retry_debate(RetryDebateCommand(accepted.debate_id, "requester", "retry-images"))
    await app.run_debate(accepted.debate_id)
    assert dependencies[6].current[accepted.debate_id].state.phase is DebatePhase.COMPLETED
    assert store.imports == 1 and outbox.image_posts == 1


@pytest.mark.asyncio
async def test_invalid_image_stops_before_any_model_calls(dependencies: Dependencies) -> None:
    events: list[str] = []
    store = ImageStore(events, fail=True)
    outbox = ImageOutbox(dependencies[6], events)
    app = make_application(
        dependencies,
        image_store=store,
        image_outbox=cast(DiscordOutboxRepository, outbox),
        outbox_recovery=cast(Any, outbox),
    )
    pending = PendingDiscordImage(
        IMAGE_ID,
        "123",
        "image/png",
        123,
        "https://cdn.discordapp.com/attachments/101/123/image.png",
    )
    accepted = await app.accept_debate(replace(request(), pending_attachments=(pending,)))
    await app.bind_discord_context(
        BindDiscordContextCommand(accepted.debate_id, "101", "102", "103")
    )
    await app.run_debate(accepted.debate_id)
    assert dependencies[6].current[accepted.debate_id].state.phase is DebatePhase.FAILED
    assert dependencies[5].affection_calls == []
    assert dependencies[4].calls == []
    assert outbox.image_posts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["import", "vision"])
async def test_image_storage_denial_stops_before_any_model_calls(
    dependencies: Dependencies, stage: str
) -> None:
    events: list[str] = []

    class DeniedStore(ImageStore):
        async def import_discord_attachment(self, pending: PendingDiscordImage) -> DebateImage:
            if stage == "import":
                raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
            return await super().import_discord_attachment(pending)

        async def load_vision_images(
            self, images: tuple[DebateImage, ...]
        ) -> tuple[ImageInput, ...]:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    store = DeniedStore(events)
    outbox = ImageOutbox(dependencies[6], events)
    app = make_application(
        dependencies,
        image_store=store,
        image_outbox=cast(DiscordOutboxRepository, outbox),
        outbox_recovery=cast(Any, outbox),
    )
    pending = PendingDiscordImage(
        IMAGE_ID,
        "123",
        "image/png",
        123,
        "https://cdn.discordapp.com/attachments/101/123/image.png",
    )
    accepted = await app.accept_debate(replace(request(), pending_attachments=(pending,)))
    await app.bind_discord_context(
        BindDiscordContextCommand(accepted.debate_id, "101", "102", "103")
    )
    await app.run_debate(accepted.debate_id)
    assert dependencies[6].current[accepted.debate_id].state.phase is DebatePhase.FAILED
    assert dependencies[5].affection_calls == []
    assert dependencies[4].calls == []


@pytest.mark.asyncio
async def test_partial_import_checkpoint_resumes_only_remaining_images(
    dependencies: Dependencies,
) -> None:
    second_id = "22345678-1234-5678-1234-abcdefabcdef"
    events: list[str] = []

    class InterruptedStore(ImageStore):
        def __init__(self) -> None:
            super().__init__(events)
            self.requests: list[str] = []
            self.interrupt = True

        async def import_discord_attachment(self, pending: PendingDiscordImage) -> DebateImage:
            self.requests.append(pending.attachment_id)
            if pending.attachment_id == second_id and self.interrupt:
                self.interrupt = False
                raise asyncio.CancelledError
            return image(pending.attachment_id)

    store = InterruptedStore()
    outbox = ImageOutbox(dependencies[6], events)
    app = make_application(
        dependencies,
        image_store=store,
        image_outbox=cast(DiscordOutboxRepository, outbox),
        outbox_recovery=cast(Any, outbox),
    )
    pending = tuple(
        PendingDiscordImage(
            identity,
            discord_id,
            "image/png",
            123,
            f"https://cdn.discordapp.com/attachments/101/{discord_id}/image.png?hm=ephemeral",
        )
        for identity, discord_id in ((IMAGE_ID, "123"), (second_id, "124"))
    )
    accepted = await app.accept_debate(replace(request(), pending_attachments=pending))
    await app.bind_discord_context(
        BindDiscordContextCommand(accepted.debate_id, "101", "102", "103")
    )
    with pytest.raises(asyncio.CancelledError):
        await app.run_debate(accepted.debate_id)
    checkpoint = dependencies[6].current[accepted.debate_id]
    assert checkpoint.attachments == (image(),)
    assert checkpoint.pending_attachments == (pending[1],)
    assert dependencies[5].affection_calls == []
    await app.run_debate(accepted.debate_id)
    completed = dependencies[6].current[accepted.debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    assert completed.attachments == (image(), image(second_id))
    assert completed.pending_attachments == ()
    assert store.requests == [IMAGE_ID, second_id, second_id]
    assert outbox.image_posts == 2


@pytest.mark.asyncio
async def test_cancel_before_image_preparation_does_not_import_or_call_models(
    dependencies: Dependencies,
) -> None:
    events: list[str] = []
    store = ImageStore(events)
    outbox = ImageOutbox(dependencies[6], events)
    app = make_application(
        dependencies,
        image_store=store,
        image_outbox=cast(DiscordOutboxRepository, outbox),
        outbox_recovery=cast(Any, outbox),
    )
    pending = PendingDiscordImage(
        IMAGE_ID,
        "123",
        "image/png",
        123,
        "https://cdn.discordapp.com/attachments/101/123/image.png",
    )
    accepted = await app.accept_debate(replace(request(), pending_attachments=(pending,)))
    await app.bind_discord_context(
        BindDiscordContextCommand(accepted.debate_id, "101", "102", "103")
    )
    await app.cancel_debate(CancelDebateCommand(accepted.debate_id, "requester", "cancel-image"))
    await app.run_debate(accepted.debate_id)
    assert dependencies[6].current[accepted.debate_id].state.phase is DebatePhase.CANCELLED
    assert store.imports == 0 and outbox.image_posts == 0
    assert dependencies[5].affection_calls == [] and dependencies[4].calls == []


@pytest.mark.asyncio
async def test_late_image_delivery_reconciliation_retains_receipts_before_advancing(
    dependencies: Dependencies,
) -> None:
    events: list[str] = []

    class LateOutbox(ImageOutbox):
        async def drain(self, *, expected: DebateSnapshot) -> None:
            await super().drain(expected=expected)
            if (
                isinstance(expected.terminal_delivery, PhaseDeliveryPlan)
                and expected.terminal_delivery.plan_id == "input-images"
            ):
                raise OutboxRecoveryAbandoned(DeliveryAbandonReason.DEADLINE_EXCEEDED.value)

    store = ImageStore(events)
    outbox = LateOutbox(dependencies[6], events)
    app = make_application(
        dependencies,
        image_store=store,
        image_outbox=cast(DiscordOutboxRepository, outbox),
        outbox_recovery=cast(Any, outbox),
    )
    accepted = await app.accept_debate(replace(request(), attachments=(image(),)))
    await app.bind_discord_context(
        BindDiscordContextCommand(accepted.debate_id, "101", "102", "103")
    )
    await app.run_debate(accepted.debate_id)
    completed = dependencies[6].current[accepted.debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    assert completed.attachment_message_ids == ((IMAGE_ID, "999"),)
    assert outbox.image_posts == 1
