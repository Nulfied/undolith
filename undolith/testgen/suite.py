"""Test-case format (portable JSON) and the argument matchers it uses.

A matcher is either a plain value (exact match) or a one-key object:

    {"eq": v}  {"glob": "data/*"}  {"regex": "^DROP"}  {"contains": "s"}  {"any": true}  {"semantic": "220"}

``semantic`` asks a judge whether two values mean the same thing. The
heuristic judge compares normalised text, and the Ollama judge asks the model.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .._canon import canonical, utcnow

SUITE_VERSION = 1
OPS = ("eq", "glob", "regex", "contains", "any", "semantic")


def is_matcher(spec: Any) -> bool:
    return isinstance(spec, dict) and len(spec) == 1 and next(iter(spec)) in OPS


def match_value(spec: Any, actual: Any, judge: Any = None, context: str = "") -> Optional[bool]:
    """True/False, or None when a semantic matcher has no judge to ask."""
    if not is_matcher(spec):
        spec = {"eq": spec}
    op, want = next(iter(spec.items()))
    if op == "eq":
        return canonical(want) == canonical(actual)
    if op == "any":
        return True
    text = actual if isinstance(actual, str) else canonical(actual)
    if op == "glob":
        return fnmatch.fnmatchcase(text.replace("\\", "/"), str(want))
    if op == "regex":
        return re.search(str(want), text) is not None
    if op == "contains":
        return str(want) in text
    if judge is None:
        return None
    return judge.equivalent(want, actual, context)


def match_call(spec: Dict[str, Any], call: Dict[str, Any], judge: Any = None, context: str = "") -> Optional[bool]:
    if not fnmatch.fnmatchcase(call.get("name", ""), spec.get("name", "*")):
        return False
    undecided = False
    for key, want in (spec.get("args") or {}).items():
        if key not in (call.get("args") or {}):
            return False
        ok = match_value(want, call["args"][key], judge, context)
        if ok is False:
            return False
        undecided = undecided or ok is None
    return None if undecided else True


def describe(spec: Dict[str, Any]) -> str:
    parts = []
    for k, v in (spec.get("args") or {}).items():
        if is_matcher(v):
            op, want = next(iter(v.items()))
            parts.append(f"{k}={want!r}" if op == "eq" else f"{k} {op} {want!r}")
        else:
            parts.append(f"{k}={v!r}")
    return f"{spec.get('name', '*')}({', '.join(parts)})"


@dataclass
class TestCase:
    __test__ = False  # not a pytest class

    id: str
    name: str
    kind: str  # "golden" (keep doing this) | "regression" (never do that again)
    task: str
    cassette: List[Dict[str, Any]] = field(default_factory=list)  # recorded {name, args, result, error}
    expect_calls: List[Dict[str, Any]] = field(default_factory=list)
    ordered: bool = False
    forbid: List[Dict[str, Any]] = field(default_factory=list)
    max_calls: Optional[int] = None
    max_repeats: Optional[int] = None  # identical (name, args) calls allowed
    final: Optional[Any] = None  # matcher for the agent's final answer
    must_pass_judge: bool = False  # the new run must not be flagged as failing by the judge
    source: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], {}, False) or k in ("id", "task")}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TestCase":
        return cls(**d)


@dataclass
class Suite:
    tests: List[TestCase] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"undolith_testgen": SUITE_VERSION, "generated_at": self.meta.get("generated_at", utcnow()),
                "meta": self.meta, "tests": [t.to_dict() for t in self.tests]}

    def save(self, path: Union[str, Path]) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str) + "\n",
                        encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Suite":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("undolith_testgen") != SUITE_VERSION:
            raise ValueError(f"{path}: not an undolith test suite (v{SUITE_VERSION})")
        return cls([TestCase.from_dict(t) for t in data.get("tests", [])], data.get("meta", {}))

    def merge(self, other: "Suite") -> "Suite":
        """Add tests from ``other`` that are not already present (by id)."""
        have = {t.id for t in self.tests}
        self.tests.extend(t for t in other.tests if t.id not in have)
        return self

    def summary(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for t in self.tests:
            out[t.kind] = out.get(t.kind, 0) + 1
        return out


def load_suite(path: Union[str, Path]) -> Suite:
    return Suite.load(path)
