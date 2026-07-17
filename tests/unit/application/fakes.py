"""Deterministic Protocol fakes for application tests."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from shittim_chest.application.models import AcceptDebateRequest, DebateSnapshot, MetricEvent
from shittim_chest.application.ports import RepositoryConflict
from shittim_chest.domain import (
    AttemptId,
    DebateId,
    EvidenceBundle,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
    Vote,
    VotingResult,
)


@dataclass(slots=True)
class FakeClock:
    current: datetime = datetime(2026, 7, 17, tzinfo=UTC)

    def now(self) -> datetime:
        value = self.current
        self.current += timedelta(microseconds=1)
        return value


@dataclass(slots=True)
class FakeIds:
    debate_ids: list[DebateId] = field(default_factory=list)
    attempt_ids: list[AttemptId] = field(default_factory=list)

    def new_debate_id(self) -> DebateId:
        value = DebateId.new()
        self.debate_ids.append(value)
        return value

    def new_attempt_id(self) -> AttemptId:
        value = AttemptId.new()
        self.attempt_ids.append(value)
        return value


@dataclass(slots=True)
class FakeMetrics:
    events: list[tuple[MetricEvent, DebateId]] = field(default_factory=list)

    def increment(self, event: MetricEvent, *, debate_id: DebateId) -> None:
        self.events.append((event, debate_id))


@dataclass(slots=True)
class FakeDiscord:
    ready: bool = True
    allowed: bool = True

    async def all_identities_ready(self) -> bool:
        return self.ready

    async def request_is_allowed(self, request: AcceptDebateRequest) -> bool:
        del request
        return self.allowed


@dataclass(slots=True)
class FakeEvidence:
    bundle: EvidenceBundle = field(default_factory=EvidenceBundle)
    calls: list[str] = field(default_factory=list)
    delay: float = 0.0

    async def prepare_evidence(self, *, question: str) -> EvidenceBundle:
        self.calls.append(question)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.bundle


@dataclass(slots=True)
class FakeCandidateOrderer:
    calls: list[tuple[ParticipantSlot, tuple[ParticipantSlot, ...]]] = field(default_factory=list)
    corrupt: bool = False
    duplicate: bool = False

    def order_candidates(
        self,
        *,
        voter: ParticipantSlot,
        candidates: tuple[FinalProposal, ...],
    ) -> tuple[FinalProposal, ...]:
        self.calls.append((voter, tuple(candidate.participant for candidate in candidates)))
        if self.corrupt:
            return candidates[:1]
        if self.duplicate:
            return (*candidates, candidates[0])
        return tuple(reversed(candidates))


class FakeOpenAI:
    def __init__(self) -> None:
        self.initial_calls: list[ParticipantSlot] = []
        self.proposal_calls: list[ParticipantSlot] = []
        self.vote_calls: list[tuple[ParticipantSlot, tuple[ParticipantSlot, ...]]] = []
        self.decision_calls: list[ParticipantSlot] = []
        self.fail_initial_for: ParticipantSlot | None = None
        self.block_initial = False
        self.cancelled_initial: set[ParticipantSlot] = set()

    async def generate_initial_opinion(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
    ) -> InitialOpinion:
        del question, evidence
        self.initial_calls.append(participant)
        if participant is self.fail_initial_for:
            await asyncio.sleep(0)
            raise RuntimeError("generated failure")
        if self.block_initial:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled_initial.add(participant)
        return InitialOpinion(participant, "summary", "proposal")

    async def generate_final_proposal(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
        initial_opinions: tuple[InitialOpinion, ...],
    ) -> FinalProposal:
        del question, evidence, initial_opinions
        self.proposal_calls.append(participant)
        return FinalProposal(participant, f"title-{participant.value}", "proposal")

    async def cast_vote(
        self,
        *,
        voter: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
        candidates: tuple[FinalProposal, ...],
    ) -> Vote:
        del question, evidence
        candidate_slots = tuple(candidate.participant for candidate in candidates)
        self.vote_calls.append((voter, candidate_slots))
        choice = {
            ParticipantSlot.PARTICIPANT_A: ParticipantSlot.PARTICIPANT_C,
            ParticipantSlot.PARTICIPANT_B: ParticipantSlot.PARTICIPANT_A,
            ParticipantSlot.PARTICIPANT_C: ParticipantSlot.PARTICIPANT_B,
        }[voter]
        return Vote(voter, choice, 3, 3, 3, "reason")

    async def generate_decision(
        self,
        *,
        question: str,
        evidence: EvidenceBundle,
        proposals: tuple[FinalProposal, ...],
        voting_result: VotingResult,
    ) -> FinalDecision:
        del question, evidence, proposals
        self.decision_calls.append(voting_result.winner)
        return FinalDecision(voting_result.winner, "decision", ("action",), ())


class FakeRepository:
    def __init__(self) -> None:
        self.current: dict[DebateId, DebateSnapshot] = {}
        self.history: dict[DebateId, list[DebateSnapshot]] = defaultdict(list)
        self.recoverable: tuple[DebateId, ...] = ()

    async def create(self, snapshot: DebateSnapshot) -> None:
        if snapshot.state.debate_id in self.current:
            raise RepositoryConflict
        self.current[snapshot.state.debate_id] = snapshot
        self.history[snapshot.state.debate_id].append(snapshot)

    async def get(self, debate_id: DebateId) -> DebateSnapshot | None:
        return self.current.get(debate_id)

    async def replace(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
    ) -> None:
        debate_id = expected.state.debate_id
        if self.current.get(debate_id) != expected:
            raise RepositoryConflict
        self.current[debate_id] = updated
        self.history[debate_id].append(updated)

    async def create_retry(
        self,
        *,
        expected_failed: DebateSnapshot,
        retry: DebateSnapshot,
    ) -> None:
        debate_id = expected_failed.state.debate_id
        if self.current.get(debate_id) != expected_failed:
            raise RepositoryConflict
        self.current[debate_id] = retry
        self.history[debate_id].append(retry)

    async def list_recoverable(self) -> tuple[DebateId, ...]:
        return self.recoverable
