"""Sequential v2 passes with a durable cursor and the existing generation fence."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from hashlib import sha256

from shittim_chest.application.errors import GenerationProviderError
from shittim_chest.application.models import (
    DebateSnapshot,
    GenerationCheckpoint,
    GenerationStatus,
    ReconsiderationProgress,
)
from shittim_chest.application.ports import (
    Clock,
    DebateRepository,
    OpenAIService,
    RepositoryConflict,
)
from shittim_chest.domain import PARTICIPANTS, InitialOpinion


async def run_reconsideration(
    snapshot: DebateSnapshot,
    *,
    coordination: bool,
    repository: DebateRepository,
    openai: OpenAIService,
    clock: Clock,
    within: Callable[[Awaitable[DebateSnapshot]], Awaitable[DebateSnapshot]],
    validate: Callable[[DebateSnapshot], None],
) -> DebateSnapshot:
    field = "candidate_coordination" if coordination else "opinion_reconsideration"
    rotation = int.from_bytes(sha256(str(snapshot.state.debate_id).encode()).digest()[:4]) % 3
    while True:
        validate(snapshot)
        progress = getattr(snapshot, field) or ReconsiderationProgress()
        if progress.step == "complete":
            return snapshot
        participant = (
            PARTICIPANTS[rotation]
            if progress.step == "classify"
            else progress.targets[progress.cursor]
        )
        checkpoint = progress.checkpoint or GenerationCheckpoint.planned(
            phase=snapshot.state.phase, participant=participant, at=clock.now()
        )
        if checkpoint.status is GenerationStatus.IN_FLIGHT and checkpoint.logical_attempt == 2:
            raise GenerationProviderError(
                "generation_attempts_exhausted",
                "reconsideration recovery exhausted",
                retryable=False,
            )
        if snapshot.lease is None:
            raise RepositoryConflict("reconsideration requires a lease")
        claimed = replace(
            progress, checkpoint=checkpoint.claim(lease=snapshot.lease, at=clock.now())
        )
        snapshot = await repository.replace(
            expected=snapshot,
            updated=replace(
                snapshot, **{field: claimed}, state=replace(snapshot.state, updated_at=clock.now())
            ),
        )
        proposed = await within(_step(snapshot, claimed, coordination, rotation, openai))
        current = await repository.get(snapshot.state.debate_id)
        if current is None or (
            current.state.attempt_id != snapshot.state.attempt_id
            or current.state.phase is not snapshot.state.phase
            or getattr(current, field) != claimed
        ):
            raise RepositoryConflict("reconsideration lost its exact generation claim")
        validate(current)
        if current.lease is None or claimed.checkpoint is None:
            raise RepositoryConflict("reconsideration result lost its lease")
        claimed.checkpoint.complete(lease=current.lease, at=clock.now())
        # Lease heartbeats may advance independently. Only apply this step's result.
        snapshot = await repository.replace(
            expected=current,
            updated=replace(
                current,
                **{field: getattr(proposed, field)},
                candidate_plans=proposed.candidate_plans,
                initial_opinions=proposed.initial_opinions,
                state=replace(current.state, updated_at=clock.now()),
            ),
        )


async def _step(
    snapshot: DebateSnapshot,
    progress: ReconsiderationProgress,
    coordination: bool,
    rotation: int,
    openai: OpenAIService,
) -> DebateSnapshot:
    field = "candidate_coordination" if coordination else "opinion_reconsideration"
    positions = (
        tuple(
            InitialOpinion(x.participant, x.selected.proposal, x.selected.proposal)
            for x in snapshot.candidate_plans
        )
        if coordination
        else snapshot.initial_opinions
    )
    if progress.step == "classify":
        targets = await openai.find_overlap_targets(
            question=snapshot.question,
            positions=positions,
            rotation=rotation,
            coordination=coordination,
        )
        updated = ReconsiderationProgress(
            step="explore" if targets else "complete", targets=targets
        )
        return replace(snapshot, **{field: updated})
    slot = progress.targets[progress.cursor]
    frame, plan = snapshot.preference_for(slot), snapshot.candidates_for(slot)
    evidence = snapshot.evidence
    if frame is None or plan is None or evidence is None:
        raise RepositoryConflict("reconsideration lost its private input")
    peers = tuple(x for x in positions if x.participant is not slot)
    next_progress = progress
    if progress.step == "explore":
        alternatives = await openai.explore_alternatives(
            question=snapshot.question, frame=frame, plan=plan, peers=peers, evidence=evidence
        )
        if alternatives:
            next_progress = replace(
                progress, step="select", alternatives=alternatives, checkpoint=None
            )
        else:
            next_progress = _after_selection(progress, coordination)
    elif progress.step == "select":
        selected = await openai.select_alternative(
            question=snapshot.question,
            frame=frame,
            plan=plan,
            peers=peers,
            evidence=evidence,
            alternatives=progress.alternatives,
        )
        if selected.participant is not slot:
            raise GenerationProviderError(
                "openai_participant_mismatch", "unexpected participant", retryable=False
            )
        snapshot = replace(
            snapshot,
            candidate_plans=tuple(
                selected if x.participant is slot else x for x in snapshot.candidate_plans
            ),
        )
        next_progress = _after_selection(progress, coordination)
    elif progress.step == "rewrite":
        score = (
            snapshot.affection_assessment.score_for(slot) if snapshot.affection_assessment else 500
        )
        opinion = await openai.revise_initial_opinion(
            question=snapshot.question,
            frame=frame,
            plan=plan,
            opinions=snapshot.initial_opinions,
            evidence=evidence,
            affection_score=score,
        )
        if opinion.participant is not slot:
            raise GenerationProviderError(
                "openai_participant_mismatch", "unexpected participant", retryable=False
            )
        snapshot = replace(
            snapshot,
            initial_opinions=tuple(
                opinion if x.participant is slot else x for x in snapshot.initial_opinions
            ),
        )
        next_progress = _next_target(progress)
    return replace(snapshot, **{field: next_progress})


def _next_target(progress: ReconsiderationProgress) -> ReconsiderationProgress:
    cursor = progress.cursor + 1
    return ReconsiderationProgress(
        step="complete" if cursor == len(progress.targets) else "explore",
        targets=progress.targets,
        cursor=cursor,
    )


def _after_selection(
    progress: ReconsiderationProgress, coordination: bool
) -> ReconsiderationProgress:
    return (
        _next_target(progress)
        if coordination
        else replace(progress, step="rewrite", alternatives=(), checkpoint=None)
    )
