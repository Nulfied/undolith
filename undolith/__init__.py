"""Undolith: simulate → commit → undo for AI agent tool calls.

Every side effect an agent makes is classified, checked against policy,
simulated with a diff, snapshotted, committed, compared with its prediction,
and written to a hash-chained, signed ledger, so it can be proven and undone.
"""

__version__ = "0.2.0"

from .approvers import AutoApprove, AutoReject, ConsoleApprover
from .classify import classify_name
from .core import ReplayStep, RollbackReport, Session, Undolith
from .model import (
    Action,
    ActionDenied,
    ActionRejected,
    Approval,
    DeviationDetected,
    Held,
    LedgerCorrupt,
    NotReversible,
    Preview,
    Risk,
    SessionHalted,
    UndoConflict,
    UndolithError,
    UnknownAction,
    UnknownOperation,
    Verdict,
)
from .ops import Adapter, Operation, Registry
from .policy import Policy, Rule
from .proof import ProofCheck, verify_proof

__all__ = [
    "Action", "ActionDenied", "ActionRejected", "Adapter", "Approval", "AutoApprove", "AutoReject",
    "ConsoleApprover", "DeviationDetected", "Held", "LedgerCorrupt", "NotReversible", "Operation", "Policy",
    "Preview", "ProofCheck", "Registry", "ReplayStep", "Risk", "RollbackReport", "Rule", "Session",
    "SessionHalted", "UndoConflict", "Undolith", "UndolithError", "UnknownAction", "UnknownOperation", "Verdict",
    "classify_name", "verify_proof", "__version__",
]
