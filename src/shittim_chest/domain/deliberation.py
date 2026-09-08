"""Private, question-scoped decision artifacts; never a learned persona profile."""

from dataclasses import dataclass, field

from shittim_chest.domain.debate_content import ParticipantSlot


def _short_text(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError("deliberation text must contain 1 to 500 characters")


@dataclass(frozen=True, slots=True)
class PreferenceFrame:
    """Ordered priorities established before considering concrete proposals."""

    participant: ParticipantSlot
    priorities: tuple[str, ...] = field(repr=False)
    avoidances: tuple[str, ...] = field(repr=False)
    compromise_condition: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.participant, ParticipantSlot):
            raise ValueError("invalid deliberation participant")
        if not isinstance(self.priorities, tuple) or not 1 <= len(self.priorities) <= 3:
            raise ValueError("deliberation requires 1 to 3 ordered priorities")
        if not isinstance(self.avoidances, tuple) or len(self.avoidances) > 3:
            raise ValueError("deliberation allows at most 3 avoidances")
        for value in (*self.priorities, *self.avoidances, self.compromise_condition):
            _short_text(value)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A concise option summary, not hidden reasoning or verified Evidence."""

    proposal: str = field(repr=False)
    fit: str = field(repr=False)
    tradeoff: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.proposal, self.fit, self.tradeoff):
            _short_text(value)


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    """Own ranked options; Python selects the first without comparing peers."""

    participant: ParticipantSlot
    candidates: tuple[Candidate, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.participant, ParticipantSlot):
            raise ValueError("invalid deliberation participant")
        if not isinstance(self.candidates, tuple) or not 1 <= len(self.candidates) <= 3:
            raise ValueError("deliberation requires 1 to 3 candidates")
        if any(not isinstance(candidate, Candidate) for candidate in self.candidates):
            raise ValueError("invalid deliberation candidate")

    @property
    def selected(self) -> Candidate:
        return self.candidates[0]
