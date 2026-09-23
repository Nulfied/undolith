"""Run generated tests against an agent, with no side effects.

The agent under test is any callable::

    def my_agent(task: str, tools: ReplayTools) -> str | None:
        out = tools.call("fs.read", path="config.yaml")
        ...
        return "final answer"

``ReplayTools`` serves recorded responses from the test's cassette, the way VCR
"cassettes" work for HTTP. Every call is recorded and then checked against the
test's expectations. Nothing real is touched. Unrecorded calls get an error
response by default, or you can pass ``on_miss`` to route them to a sandboxed
Undolith.
"""

from __future__ import annotations

import importlib
import sys
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from .._canon import canonical
from .judge import FAIL, HeuristicJudge, Judge, failing
from .suite import Suite, TestCase, describe, match_call, match_value
from .trace import ERROR, OK, Step, Trace

Agent = Callable[[str, "ReplayTools"], Any]


class UnrecordedCall(LookupError):
    pass


class LiveTools:
    """Production counterpart of :class:`ReplayTools`, with the same ``call`` interface, backed by an Undolith session.

    Write the agent once against ``tools.call(...)``: run it live with ``LiveTools(session)``
    (guarded, recorded, undoable) and in tests with the ``ReplayTools`` that ``run_test`` passes in.
    Denials, holds and rollbacks come back as text, so the agent can react instead of crashing.
    """

    def __init__(self, session: Any):
        self.session = session

    def call(self, name: str, /, **args: Any) -> Any:
        from ..integrations.function_calls import dispatch

        return dispatch(self.session.guard, name, args, session=self.session, as_text=False)

    def __getitem__(self, name: str) -> Callable[..., Any]:
        return lambda **args: self.call(name, **args)


class ReplayTools:
    def __init__(self, cassette: List[Dict[str, Any]], *, on_miss: Union[str, Callable[..., Any]] = "error"):
        self._exact: Dict[str, List[Dict[str, Any]]] = {}
        self._by_name: Dict[str, List[Dict[str, Any]]] = {}
        for rec in cassette:
            self._exact.setdefault(f"{rec['name']}:{canonical(rec.get('args') or {})}", []).append(rec)
            self._by_name.setdefault(rec["name"], []).append(rec)
        self.on_miss = on_miss
        self.calls: List[Dict[str, Any]] = []

    def call(self, name: str, /, **args: Any) -> Any:
        queue = self._exact.get(f"{name}:{canonical(args)}")
        served, rec = "miss", None
        if queue:
            rec = queue.pop(0) if len(queue) > 1 else queue[0]  # replay in order; the last one repeats
            served = "exact"
        entry: Dict[str, Any] = {"name": name, "args": args, "served": served}
        self.calls.append(entry)
        if rec is not None:
            entry["result"] = rec.get("result")
            if rec.get("error"):
                entry["error"] = rec["error"]
            return rec.get("result")
        if callable(self.on_miss):
            entry["served"] = "on_miss"
            entry["result"] = self.on_miss(name, args)
            return entry["result"]
        if self.on_miss == "raise":
            raise UnrecordedCall(f"no recorded response for {name}({canonical(args)})")
        entry["error"] = "unrecorded"
        known = sorted(self._by_name)
        return {"error": f"no recorded response for {name} with these arguments", "recorded_tools": known}

    def __getitem__(self, name: str) -> Callable[..., Any]:
        return lambda **args: self.call(name, **args)

    def functions(self) -> Dict[str, Callable[..., Any]]:
        """``{tool_name: callable}`` for plugging into frameworks that want plain functions."""
        return {n: self[n] for n in self._by_name}


@dataclass
class TestResult:
    __test__ = False

    test: TestCase
    passed: bool
    failures: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    calls: List[Dict[str, Any]] = field(default_factory=list)
    final: Any = None

    def explain(self) -> str:
        lines = [f"{'PASS' if self.passed else 'FAIL'} {self.test.id} {self.test.name}"]
        lines += [f"  ✗ {f}" for f in self.failures]
        lines += [f"  ~ skipped: {s}" for s in self.skipped]
        if not self.passed:
            lines.append(f"  task: {self.test.task[:200]!r}")
            lines += [f"  call {i + 1}: {c['name']}({canonical(c['args'])[:120]}) [{c['served']}]"
                      for i, c in enumerate(self.calls[:30])]
            lines.append(f"  final: {str(self.final)[:200]!r}")
            src = self.test.source
            if src.get("reason"):
                lines.append(f"  originally: {src.get('reason')} (trace {src.get('trace')}, judge {src.get('judge')})")
        return "\n".join(lines)


def run_test(test: TestCase, agent: Agent, *, judge: Optional[Judge] = None,
             on_miss: Union[str, Callable[..., Any]] = "error") -> TestResult:
    judge = judge or HeuristicJudge()
    tools = ReplayTools(test.cassette, on_miss=on_miss)
    failures: List[str] = []
    skipped: List[str] = []
    final = None
    try:
        final = agent(test.task, tools)
    except Exception as exc:
        failures.append(f"agent raised {type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
    calls = tools.calls

    pos = 0
    for spec in test.expect_calls:
        start = pos if test.ordered else 0
        hit, undecided = None, False
        for i in range(start, len(calls)):
            ok = match_call(spec, calls[i], judge, test.task)
            if ok:
                hit = i
                break
            undecided = undecided or ok is None
        if hit is not None:
            pos = hit + 1
        elif undecided:
            skipped.append(f"could not evaluate expected call {describe(spec)} (judge undecided)")
        else:
            failures.append(f"expected call not made{' in order' if test.ordered else ''}: {describe(spec)}")

    for spec in test.forbid:
        for c in calls:
            ok = match_call(spec, c, judge, test.task)
            if ok:
                failures.append(f"forbidden call made: {c['name']}({canonical(c['args'])[:150]})"
                                + (f" (why forbidden: {spec['reason']})" if spec.get("reason") else ""))
                break
            if ok is None:
                skipped.append(f"could not evaluate forbidden call {describe(spec)}")

    if test.max_calls is not None and len(calls) > test.max_calls:
        failures.append(f"{len(calls)} tool calls (limit {test.max_calls})")
    if test.max_repeats is not None:
        counts = Counter(f"{c['name']}:{canonical(c['args'])}" for c in calls)
        worst = counts.most_common(1)
        if worst and worst[0][1] > test.max_repeats:
            failures.append(f"identical call repeated {worst[0][1]}x (limit {test.max_repeats}): {worst[0][0][:150]}")

    if test.final is not None:
        if final is None:
            failures.append("no final answer")
        else:
            ok = match_value(test.final, final, judge, test.task)
            if ok is None:
                skipped.append("final answer not evaluated (judge undecided)")
            elif not ok:
                failures.append(f"final answer {str(final)[:150]!r} does not match {test.final!r}")

    if test.must_pass_judge:
        steps = [Step(name=c["name"], args=c["args"], result=c.get("result"), error=c.get("error"),
                      status=ERROR if c.get("error") else OK) for c in calls]
        run = Trace(id=f"run-{test.id}", task=test.task, steps=steps,
                    final=None if final is None else str(final), source="testgen-run")
        for f in failing(judge.judge(run)):
            failures.append(f"judge ({f.judge}) flags the new run: {f.reason}")

    return TestResult(test, not failures, failures, skipped, calls, final)


@dataclass
class SuiteReport:
    results: List[TestResult]

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def explain(self, only_failures: bool = True) -> str:
        body = [r.explain() for r in self.results if not (only_failures and r.passed)]
        return "\n\n".join(body + [f"{self.passed} passed, {self.failed} failed"])


def run_suite(suite: Union[Suite, str, Path], agent: Agent, *, judge: Optional[Judge] = None,
              on_miss: Union[str, Callable[..., Any]] = "error") -> SuiteReport:
    suite = suite if isinstance(suite, Suite) else Suite.load(suite)
    return SuiteReport([run_test(t, agent, judge=judge, on_miss=on_miss) for t in suite.tests])


def load_agent(spec: str) -> Agent:
    """``package.module:function`` → the callable (imported relative to the current directory)."""
    module, _, attr = spec.partition(":")
    if not attr:
        raise ValueError(f"agent spec must look like module:function, got {spec!r}")
    sys.path.insert(0, str(Path.cwd()))
    obj: Any = importlib.import_module(module)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


PYTEST_TEMPLATE = '''"""Agent regression tests generated by undolith testgen. Regenerate, don't hand-edit.

Suite: {suite}  ({count} tests: {summary})
"""
from pathlib import Path

import pytest

from undolith.testgen import Suite, load_agent, make_judge, run_test

agent = load_agent({agent_spec!r})
SUITE = Suite.load(Path(__file__).with_name({suite_name!r}))
JUDGE = make_judge({judge!r})


@pytest.mark.parametrize("case", SUITE.tests, ids=[t.id for t in SUITE.tests])
def test_agent(case):
    result = run_test(case, agent, judge=JUDGE)
    assert result.passed, result.explain()
'''


def export_pytest(suite_path: Union[str, Path], agent_spec: str, out: Union[str, Path], *,
                  judge: str = "heuristic") -> Path:
    """Write a pytest file (next to a copy of the suite) that runs every test against ``agent_spec``."""
    suite_path, out = Path(suite_path), Path(out)
    if ":" not in agent_spec:
        raise ValueError(f"agent spec must look like module:function, got {agent_spec!r}")
    suite = Suite.load(suite_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    target = out.with_name(out.stem + ".suite.json")
    suite.save(target)
    summary = ", ".join(f"{v} {k}" for k, v in sorted(suite.summary().items())) or "empty"
    out.write_text(PYTEST_TEMPLATE.format(suite=suite_path.name, count=len(suite.tests), summary=summary,
                                          agent_spec=agent_spec, suite_name=target.name, judge=judge), encoding="utf-8")
    return out


__all__ = ["FAIL", "LiveTools", "ReplayTools", "SuiteReport", "TestResult", "UnrecordedCall", "export_pytest", "load_agent",
           "run_suite", "run_test"]
