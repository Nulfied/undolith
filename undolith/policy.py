"""The policy / permission engine.

Decision order for every action:

1. The first matching rule gives the base verdict; otherwise the default for its risk.
2. ``deny`` is final. Reads are never escalated.
3. Escalations only ever make a verdict stricter:
   * destructive-or-worse and not reversible  -> at least ``approve``
   * ``simulate`` requested but no simulator   -> ``approve`` (if ``require_simulator``)
   * blast-radius limits exceeded for the session -> at least ``approve``
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from ._canon import digest
from .model import Action, Risk, Verdict

DEFAULTS = {
    Risk.READ: Verdict.ALLOW,
    Risk.WRITE: Verdict.SIMULATE,
    Risk.DESTRUCTIVE: Verdict.SIMULATE,
    Risk.IRREVERSIBLE: Verdict.APPROVE,
}
DEVIATION_RESPONSES = ("warn", "halt", "rollback", "rollback_session")
DEFAULT_REDACT = ("password", "passwd", "secret", "token", "api_key", "apikey", "authorization", "cookie",
                  "private_key", "credential", "ssn", "card_number", "cvv")


@dataclass
class Rule:
    """``match`` is a glob on ``tool.op``; ``args`` maps argument names to globs."""

    verdict: Verdict
    match: str = "*"
    min_risk: Risk = Risk.READ
    max_risk: Risk = Risk.IRREVERSIBLE
    args: Dict[str, str] = field(default_factory=dict)
    when: Optional[Callable[[Action], bool]] = None
    reason: str = ""

    def __post_init__(self):
        self.verdict = Verdict.parse(self.verdict)
        self.min_risk = Risk.parse(self.min_risk)
        self.max_risk = Risk.parse(self.max_risk)

    def matches(self, action: Action) -> bool:
        if not fnmatch.fnmatchcase(action.qualname, self.match):
            return False
        if not (self.min_risk <= action.risk <= self.max_risk):
            return False
        for name, pattern in self.args.items():
            if name not in action.args or not fnmatch.fnmatchcase(str(action.args[name]), pattern):
                return False
        return self.when is None or bool(self.when(action))

    def describe(self) -> str:
        return self.reason or f"rule {self.match} -> {self.verdict.value}"

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"match": self.match, "verdict": self.verdict.value}
        if self.min_risk != Risk.READ:
            d["min_risk"] = self.min_risk.label
        if self.max_risk != Risk.IRREVERSIBLE:
            d["max_risk"] = self.max_risk.label
        if self.args:
            d["args"] = dict(self.args)
        if self.when is not None:
            d["when"] = getattr(self.when, "__qualname__", repr(self.when))
        if self.reason:
            d["reason"] = self.reason
        return d


@dataclass
class Policy:
    rules: List[Rule] = field(default_factory=list)
    defaults: Dict[Risk, Verdict] = field(default_factory=lambda: dict(DEFAULTS))
    max_destructive_per_session: Optional[int] = 5
    max_mutations_per_session: Optional[int] = None
    approve_irreversible_destructive: bool = True
    require_simulator: bool = False
    deviation_threshold: float = 0.0
    on_deviation: str = "rollback"
    redact: Sequence[str] = DEFAULT_REDACT
    log_reads: bool = True

    def __post_init__(self):
        self.defaults = {Risk.parse(k): Verdict.parse(v) for k, v in {**DEFAULTS, **self.defaults}.items()}
        self.rules = [r if isinstance(r, Rule) else Rule(**r) for r in self.rules]
        if self.on_deviation not in DEVIATION_RESPONSES:
            raise ValueError(f"on_deviation must be one of {DEVIATION_RESPONSES}")

    # -- construction -----------------------------------------------------------------
    @classmethod
    def permissive(cls) -> "Policy":
        """Commit everything, still logged and snapshotted. Useful for sandboxes and replays."""
        return cls(defaults={r: Verdict.ALLOW for r in Risk}, max_destructive_per_session=None,
                   approve_irreversible_destructive=False)

    @classmethod
    def strict(cls) -> "Policy":
        """Every mutation needs approval."""
        return cls(defaults={Risk.READ: Verdict.ALLOW, Risk.WRITE: Verdict.APPROVE,
                             Risk.DESTRUCTIVE: Verdict.APPROVE, Risk.IRREVERSIBLE: Verdict.HOLD},
                   require_simulator=True)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Policy":
        dev = d.get("deviation", {})
        kw: Dict[str, Any] = {
            "rules": [Rule(**r) for r in d.get("rules", [])],
            "defaults": {Risk.parse(k): Verdict.parse(v) for k, v in d.get("defaults", {}).items()},
        }
        for key in ("max_destructive_per_session", "max_mutations_per_session",
                    "approve_irreversible_destructive", "require_simulator", "log_reads"):
            if key in d:
                kw[key] = d[key]
        if "redact" in d:
            kw["redact"] = tuple(d["redact"])
        if "threshold" in dev:
            kw["deviation_threshold"] = float(dev["threshold"])
        if "on" in dev:
            kw["on_deviation"] = dev["on"]
        return cls(**kw)

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Policy":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "defaults": {r.label: v.value for r, v in sorted(self.defaults.items())},
            "rules": [r.to_dict() for r in self.rules],
            "max_destructive_per_session": self.max_destructive_per_session,
            "max_mutations_per_session": self.max_mutations_per_session,
            "approve_irreversible_destructive": self.approve_irreversible_destructive,
            "require_simulator": self.require_simulator,
            "deviation": {"threshold": self.deviation_threshold, "on": self.on_deviation},
            "redact": list(self.redact),
            "log_reads": self.log_reads,
        }

    @property
    def fingerprint(self) -> str:
        """Hash of the policy in force; recorded with every proposed action."""
        return digest(self.to_dict())

    # -- decisions --------------------------------------------------------------------
    def add(self, *rules: Rule) -> "Policy":
        self.rules.extend(rules)
        return self

    def decide(self, action: Action, stats: Any, *, reversible: bool, has_simulator: bool) -> Tuple[Verdict, str]:
        verdict, reason = None, ""
        for rule in self.rules:
            if rule.matches(action):
                verdict, reason = rule.verdict, rule.describe()
                break
        if verdict is None:
            verdict, reason = self.defaults[action.risk], f"default for {action.risk.label} actions"
        if verdict is Verdict.DENY or action.risk is Risk.READ:
            return verdict, reason

        def escalate(to: Verdict, why: str):
            nonlocal verdict, reason
            if to.rank > verdict.rank:
                verdict, reason = to, f"{reason}; {why}"

        if action.risk >= Risk.DESTRUCTIVE and not reversible and self.approve_irreversible_destructive:
            escalate(Verdict.APPROVE, "no inverse registered, cannot be undone")
        if verdict is Verdict.SIMULATE and not has_simulator and self.require_simulator:
            escalate(Verdict.APPROVE, "no simulator available")
        limit = self.max_destructive_per_session
        if limit is not None and action.risk >= Risk.DESTRUCTIVE and stats.destructive >= limit:
            escalate(Verdict.APPROVE, f"blast radius: {stats.destructive} destructive actions already this session")
        limit = self.max_mutations_per_session
        if limit is not None and stats.mutations >= limit:
            escalate(Verdict.APPROVE, f"blast radius: {stats.mutations} mutations already this session")
        return verdict, reason

    def redact_value(self, key: str) -> bool:
        k = key.lower()
        return any(p in k for p in self.redact)
