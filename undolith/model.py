"""Core value types and errors."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


class Risk(enum.IntEnum):
    """How dangerous an action is. Ordered: higher is riskier."""

    READ = 0  # no side effects
    WRITE = 1  # side effects that can normally be undone
    DESTRUCTIVE = 2  # removes or overwrites data
    IRREVERSIBLE = 3  # leaves the system: sends, payments, publishes, pushes

    @classmethod
    def parse(cls, value: Any) -> "Risk":
        if isinstance(value, Risk):
            return value
        if isinstance(value, int):
            return cls(value)
        if isinstance(value, str):
            try:
                return cls[value.strip().upper()]
            except KeyError:
                raise ValueError(f"unknown risk {value!r}; use read/write/destructive/irreversible") from None
        raise TypeError(f"cannot interpret {value!r} as a Risk")

    @property
    def label(self) -> str:
        return self.name.lower()


class Verdict(str, enum.Enum):
    """What the policy engine decided to do with an action. Ordered by strictness."""

    ALLOW = "allow"  # commit directly (still logged, still snapshotted)
    SIMULATE = "simulate"  # simulate + preview, then commit automatically
    APPROVE = "approve"  # simulate, then ask the approver (held if none)
    HOLD = "hold"  # simulate and park in the outbox until released
    DENY = "deny"  # never run

    @property
    def rank(self) -> int:
        return _RANK[self]

    @classmethod
    def parse(cls, value: Any) -> "Verdict":
        return value if isinstance(value, Verdict) else cls(str(value).strip().lower())

    def at_least(self, other: "Verdict") -> "Verdict":
        return self if self.rank >= other.rank else other


_RANK = {Verdict.ALLOW: 0, Verdict.SIMULATE: 1, Verdict.APPROVE: 2, Verdict.HOLD: 3, Verdict.DENY: 4}


@dataclass
class Preview:
    """What a simulation predicts an action will do."""

    summary: str
    diff: str = ""
    predicted: Dict[str, Any] = field(default_factory=dict)  # observable post-state, compared after commit
    simulated: bool = True  # False when no simulator exists and effects are unknown
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Action:
    id: str
    session: str
    agent: str
    tool: str
    op: str
    args: Dict[str, Any]
    risk: Risk

    @property
    def qualname(self) -> str:
        return f"{self.tool}.{self.op}"


@dataclass
class Approval:
    approved: bool
    by: str = "approver"
    note: str = ""


@dataclass
class Held:
    """Returned instead of a result when an action is parked for later release."""

    action_id: str
    qualname: str
    preview: Optional[Preview]
    reason: str

    def __repr__(self) -> str:
        return f"<Held {self.qualname} {self.action_id}: {self.reason}>"


class UndolithError(Exception):
    """Base class for all Undolith errors."""


class UnknownOperation(UndolithError, KeyError):
    pass


class UnknownAction(UndolithError, KeyError):
    pass


class ActionDenied(UndolithError):
    def __init__(self, action_id: str, reason: str):
        super().__init__(f"{action_id} denied by policy: {reason}")
        self.action_id, self.reason = action_id, reason


class ActionRejected(UndolithError):
    def __init__(self, action_id: str, reason: str):
        super().__init__(f"{action_id} rejected: {reason}")
        self.action_id, self.reason = action_id, reason


class SessionHalted(UndolithError):
    pass


class NotReversible(UndolithError):
    pass


class UndoConflict(UndolithError):
    """The world changed since the action committed; undoing would clobber newer state."""

    def __init__(self, action_id: str, expected: Any, current: Any):
        super().__init__(
            f"{action_id}: state drifted since commit (expected {expected!r}, found {current!r}); "
            "roll back later actions first or pass force=True"
        )
        self.action_id, self.expected, self.current = action_id, expected, current


class DeviationDetected(UndolithError):
    """The real outcome differed from the simulated prediction."""

    def __init__(self, action_id: str, mismatches: Dict[str, Any], response: str):
        super().__init__(f"{action_id}: outcome deviated from simulation ({response}): {mismatches}")
        self.action_id, self.mismatches, self.response = action_id, mismatches, response


class LedgerCorrupt(UndolithError):
    pass
