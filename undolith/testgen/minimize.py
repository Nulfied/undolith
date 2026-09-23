"""Shrink failing tests and traces to their essence (delta debugging).

* ``minimize_test(test, agent)`` removes cassette entries until only the ones
  needed for ``agent`` to still fail the test *in the same way* are left. A small
  test is easier to read, less brittle, and shows exactly what triggers the bug.
  If the agent passes the test, the test does not reproduce anything for that
  agent. That is worth knowing too.
* ``minimize_trace(trace, judge)`` finds the shortest prefix of a trace that the
  judge still flags. That is where things started going wrong.

The algorithm is Zeller's ddmin: it keeps shrinking until removing any single
remaining entry makes the failure disappear.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Callable, FrozenSet, List, Optional, Sequence, TypeVar

from .judge import Judge, failing
from .runner import Agent, run_test
from .suite import TestCase
from .trace import Trace

T = TypeVar("T")

_KINDS = (("forbidden call made", "forbidden"), ("expected call not made", "expected"), ("tool calls (limit", "max_calls"),
          ("identical call repeated", "repeats"), ("final answer", "final"), ("no final answer", "final"),
          ("judge (", "judge"), ("agent raised", "crash"))


def failure_signature(failures: Sequence[str]) -> FrozenSet[str]:
    """The *kinds* of failure, so a minimised test must fail for the same reasons, not just fail."""
    kinds = set()
    for f in failures:
        kinds.add(next((k for prefix, k in _KINDS if f.startswith(prefix) or prefix in f[:40]), "other"))
    return frozenset(kinds)


def ddmin(items: List[T], still_fails: Callable[[List[T]], bool]) -> List[T]:
    """Smallest (1-minimal) sub-list for which ``still_fails`` holds. Assumes it holds for ``items``."""
    if still_fails([]):
        return []
    n = 2
    while len(items) >= 2:
        size = -(-len(items) // n)
        chunks = [items[i:i + size] for i in range(0, len(items), size)]
        for i, chunk in enumerate(chunks):
            if still_fails(chunk):
                items, n = chunk, 2
                break
            rest = [x for j, c in enumerate(chunks) if j != i for x in c]
            if n > 2 and still_fails(rest):
                items, n = rest, max(n - 1, 2)
                break
        else:
            if n >= len(items):
                break
            n = min(len(items), n * 2)
    return items


@dataclass
class Minimized:
    test: TestCase
    reproduces: bool
    before: int
    after: int
    runs: int
    signature: FrozenSet[str] = frozenset()

    def describe(self) -> str:
        if not self.reproduces:
            return f"{self.test.id}: agent passes this test; nothing to minimise"
        return (f"{self.test.id}: cassette {self.before} -> {self.after} entries in {self.runs} runs "
                f"(still fails with {', '.join(sorted(self.signature))})")


def minimize_test(test: TestCase, agent: Agent, *, judge: Optional[Judge] = None) -> Minimized:
    runs = 0

    def run(cassette) -> Any:
        nonlocal runs
        runs += 1
        return run_test(dataclasses.replace(test, cassette=list(cassette)), agent, judge=judge)

    first = run(test.cassette)
    if first.passed:
        return Minimized(test, False, len(test.cassette), len(test.cassette), runs)
    target = failure_signature(first.failures)
    keep = ddmin(list(range(len(test.cassette))),
                 lambda idx: failure_signature(run([test.cassette[i] for i in idx]).failures) == target)
    small = dataclasses.replace(test, cassette=[test.cassette[i] for i in keep],
                                source={**test.source, "minimized_from": len(test.cassette)})
    return Minimized(small, True, len(test.cassette), len(keep), runs, target)


def minimize_trace(trace: Trace, judge: Judge) -> Optional[Trace]:
    """Shortest prefix of ``trace`` that ``judge`` still flags as failing, or None if it is not flagged."""
    if not failing(judge.judge(trace)):
        return None
    lo, hi = 0, len(trace.steps)  # invariant: prefix of length hi fails
    while lo < hi:
        mid = (lo + hi) // 2
        cut = dataclasses.replace(trace, steps=trace.steps[:mid], final=None if mid < len(trace.steps) else trace.final)
        if failing(judge.judge(cut)):
            hi = mid
        else:
            lo = mid + 1
    return dataclasses.replace(trace, steps=trace.steps[:hi], final=None if hi < len(trace.steps) else trace.final,
                               meta={**trace.meta, "minimized_from": len(trace.steps)})
