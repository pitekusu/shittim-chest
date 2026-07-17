"""Immutable debate content and deterministic voting rules."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Final


@unique
class ParticipantSlot(StrEnum):
    """Stable public identifiers for the three voting participants."""

    PARTICIPANT_A = "participant-a"
    PARTICIPANT_B = "participant-b"
    PARTICIPANT_C = "participant-c"


@unique
class SearchRequirement(StrEnum):
    """Whether current external evidence is needed for one question."""

    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


@unique
class EvidenceSearchStatus(StrEnum):
    """Persisted outcome of the one evidence preparation request."""

    NOT_REQUESTED = "not_requested"
    COMPLETED = "completed"
    OPTIONAL_UNAVAILABLE = "optional_unavailable"


PARTICIPANTS: Final[tuple[ParticipantSlot, ...]] = tuple(ParticipantSlot)
STABLE_TIE_BREAK_ORDER: Final[tuple[ParticipantSlot, ...]] = (
    ParticipantSlot.PARTICIPANT_B,
    ParticipantSlot.PARTICIPANT_A,
    ParticipantSlot.PARTICIPANT_C,
)


class InvalidVote(ValueError):
    """Raised when a vote violates a stable domain invariant."""

    __slots__ = ("code",)

    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One untrusted evidence item shared identically with every participant."""

    source_url: str
    title: str
    source_metadata: str
    retrieved_at: str
    content_hash: str

    def __post_init__(self) -> None:
        for label, value in (
            ("source URL", self.source_url),
            ("title", self.title),
            ("retrieved at", self.retrieved_at),
            ("content hash", self.content_hash),
        ):
            if not value.strip():
                raise ValueError(f"{label} must not be empty")


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """The immutable evidence set prepared once per debate."""

    items: tuple[EvidenceItem, ...] = ()
    required_search_satisfied: bool = True
    summary: str = ""
    search_requirement: SearchRequirement = SearchRequirement.NONE
    search_status: EvidenceSearchStatus = EvidenceSearchStatus.NOT_REQUESTED
    search_response_id: str | None = None
    router_rules_version: str = "not-routed-v0"
    routing_reason: str = "not_routed"

    def __post_init__(self) -> None:
        if not self.router_rules_version.strip() or not self.routing_reason.strip():
            raise ValueError("router version and routing reason must not be empty")
        if self.search_requirement is SearchRequirement.NONE:
            if self.search_status is not EvidenceSearchStatus.NOT_REQUESTED:
                raise ValueError("a no-search bundle must be marked not requested")
            if self.items or self.summary or self.search_response_id is not None:
                raise ValueError("a no-search bundle must not contain search output")
        elif self.search_status is EvidenceSearchStatus.COMPLETED:
            if not self.items or not self.summary.strip() or not self.search_response_id:
                raise ValueError(
                    "completed search evidence must contain summary, sources, and response ID"
                )
            if not self.required_search_satisfied:
                raise ValueError("completed search evidence must be satisfied")
        elif (
            self.search_requirement is SearchRequirement.OPTIONAL
            and self.search_status is EvidenceSearchStatus.OPTIONAL_UNAVAILABLE
        ):
            if self.items or self.summary or self.search_response_id is not None:
                raise ValueError("unavailable optional evidence must not contain search output")
            if self.required_search_satisfied:
                raise ValueError("unavailable optional evidence must be marked unsatisfied")
        else:
            raise ValueError("invalid evidence search requirement and status")


@dataclass(frozen=True, slots=True)
class InitialOpinion:
    """A participant's first opinion."""

    participant: ParticipantSlot
    summary: str
    proposal: str

    def __post_init__(self) -> None:
        _require_participant(self.participant)
        _require_text(self.summary, label="opinion summary")
        _require_text(self.proposal, label="opinion proposal")


@dataclass(frozen=True, slots=True)
class FinalProposal:
    """A participant's final candidate proposal."""

    participant: ParticipantSlot
    title: str
    proposal: str

    def __post_init__(self) -> None:
        _require_participant(self.participant)
        _require_text(self.title, label="proposal title")
        _require_text(self.proposal, label="proposal")


@dataclass(frozen=True, slots=True)
class Vote:
    """One validated vote for another participant's final proposal."""

    voter: ParticipantSlot
    candidate: ParticipantSlot
    accuracy_score: int
    usefulness_score: int
    safety_score: int
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.voter, ParticipantSlot):
            raise InvalidVote("unknown_voter", "vote voter must be a known participant")
        if not isinstance(self.candidate, ParticipantSlot):
            raise InvalidVote("unknown_candidate", "vote candidate must be a known participant")
        if self.voter is self.candidate:
            raise InvalidVote("self_vote", "a participant cannot vote for itself")
        for label, score in (
            ("accuracy", self.accuracy_score),
            ("usefulness", self.usefulness_score),
            ("safety", self.safety_score),
        ):
            if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
                raise InvalidVote("score_out_of_range", f"{label} score must be between 1 and 5")
        _require_text(self.reason, label="vote reason")
        if len(self.reason) > 500:
            raise InvalidVote("reason_too_long", "vote reason must be at most 500 characters")

    @property
    def total_score(self) -> int:
        """Return the documented aggregate score for tie breaking."""

        return self.accuracy_score + self.usefulness_score + self.safety_score


@dataclass(frozen=True, slots=True)
class VotingResult:
    """Deterministic winner and the validated complete ballot."""

    winner: ParticipantSlot
    votes: tuple[Vote, ...]


@dataclass(frozen=True, slots=True)
class FinalDecision:
    """The final decision generated from the mechanically selected winner."""

    winner: ParticipantSlot
    decision: str
    actions: tuple[str, ...]
    caveats: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_participant(self.winner)
        _require_text(self.decision, label="decision")


def select_winner(votes: Iterable[Vote]) -> VotingResult:
    """Validate a complete ballot and select its winner deterministically."""

    ballot = tuple(votes)
    if len(ballot) != len(PARTICIPANTS):
        raise InvalidVote("incomplete_ballot", "exactly one vote per participant is required")

    voters = tuple(vote.voter for vote in ballot)
    if len(set(voters)) != len(voters):
        raise InvalidVote("duplicate_voter", "each participant may vote only once")
    if set(voters) != set(PARTICIPANTS):
        raise InvalidVote("unknown_voter", "the ballot contains an unknown or missing voter")

    vote_counts = Counter(vote.candidate for vote in ballot)
    highest_count = max(vote_counts.values())
    leaders = tuple(
        participant for participant in PARTICIPANTS if vote_counts[participant] == highest_count
    )
    if len(leaders) == 1:
        return VotingResult(winner=leaders[0], votes=ballot)

    winner = min(leaders, key=lambda participant: _tie_break_key(participant, ballot))
    return VotingResult(winner=winner, votes=ballot)


def _tie_break_key(
    participant: ParticipantSlot,
    votes: tuple[Vote, ...],
) -> tuple[int, int, int, int, int]:
    received = tuple(vote for vote in votes if vote.candidate is participant)
    return (
        -sum(vote.total_score for vote in received),
        -sum(vote.accuracy_score for vote in received),
        -sum(vote.safety_score for vote in received),
        -sum(vote.usefulness_score for vote in received),
        STABLE_TIE_BREAK_ORDER.index(participant),
    )


def _require_text(value: str, *, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")


def _require_participant(value: ParticipantSlot) -> None:
    if not isinstance(value, ParticipantSlot):
        raise ValueError("participant must be a known participant slot")
