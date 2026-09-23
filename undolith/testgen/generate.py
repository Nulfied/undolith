"""Traces + findings → test cases.

* **Golden** test, from a clean trace: replaying the same task with the same
  recorded tool responses, the agent should make the same mutating calls and
  reach the same final answer.
* **Regression** test, from each failure: put the agent back in the same
  situation (same task, same recorded responses) and assert it does *not*
  repeat the failing call, loop, or give up.

Blocked, undone and deviating calls from an Undolith ledger are failures that
are already labelled, so they become regression tests without any judge.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from .. import __version__
from .._canon import digest, short, utcnow
from ..classify import classify_name
from ..model import Risk
from .judge import FAIL, Finding, HeuristicJudge, Judge, failing
from .suite import Suite, TestCase, describe
from .trace import OK, REPLAYABLE, Step, Trace

IDENTIFYING_ARGS = ("path", "src", "dst", "file", "url", "method", "to", "sql", "cmd", "command", "name", "id",
                    "table", "recipient", "repo", "branch", "input", "query", "target", "key")


def _is_read(step: Step) -> bool:
    risk = step.risk or classify_name(step.name.rsplit(".", 1)[-1]).label
    return Risk.parse(risk) is Risk.READ


def generalize_value(value: Any) -> Any:
    """``data/salaries.csv`` → ``{"glob": "data/*"}``; anything else matches exactly."""
    if isinstance(value, str) and "\n" not in value and len(value) < 300:
        v = value.replace("\\", "/")
        if "/" in v.strip("/") and not v.startswith(("http://", "https://")):
            return {"glob": v.rsplit("/", 1)[0] + "/*"}
    return {"eq": value}


def call_spec(step: Step, *, generalize: bool = False, identifying_only: bool = True) -> Dict[str, Any]:
    keys = [k for k in step.args if k in IDENTIFYING_ARGS] if identifying_only else []
    keys = keys or list(step.args)
    args = {k: (generalize_value(step.args[k]) if generalize else {"eq": step.args[k]}) for k in keys}
    return {"name": step.name, "args": args}


def cassette_for(trace: Trace) -> List[Dict[str, Any]]:
    """Recorded responses the agent under test is served. Blocked/undone calls are left out."""
    out = []
    for s in trace.steps:
        if s.status in REPLAYABLE:
            entry: Dict[str, Any] = {"name": s.name, "args": s.args, "result": s.result}
            if s.error:
                entry["error"] = s.error
            out.append(entry)
    return out


def _source(trace: Trace, finding: Optional[Finding] = None) -> Dict[str, Any]:
    src: Dict[str, Any] = {"trace": trace.id, "origin": trace.source}
    if finding is not None:
        src.update({"judge": finding.judge, "rule": finding.rule, "reason": finding.reason})
        if finding.step is not None:
            src["step"] = finding.step
    return src


def golden_test(trace: Trace, *, include_reads: bool = False, semantic_args: bool = False) -> Optional[TestCase]:
    expected = [s for s in trace.steps if s.status == OK and (include_reads or not _is_read(s))]
    if not expected and trace.final is None:
        return None
    specs = []
    for s in expected:
        spec = call_spec(s, identifying_only=False)
        if semantic_args:
            spec["args"] = {k: {"semantic": v["eq"]} for k, v in spec["args"].items()}
        specs.append(spec)
    body = {"kind": "golden", "task": trace.task, "expect": specs, "final": trace.final}
    return TestCase(
        id="g_" + digest(body)[:12],
        name=f"golden: {short(trace.task or trace.id, 70)}",
        kind="golden", task=trace.task, cassette=cassette_for(trace), expect_calls=specs, ordered=True,
        max_calls=max(2 * len(trace.steps), len(trace.steps) + 5),
        final={"semantic": trace.final} if trace.final is not None else None,
        source=_source(trace),
    )


def regression_test(trace: Trace, finding: Finding, *, generalize: bool = True) -> TestCase:
    step = trace.steps[finding.step] if finding.step is not None else None
    kw: Dict[str, Any] = {}
    if finding.rule == "loop" and step is not None:
        kw["max_repeats"] = 2
        what = f"loop on {step.name}"
    elif step is not None:
        spec = call_spec(step, generalize=generalize)
        kw["forbid"] = [{**spec, "reason": finding.reason}]
        what = f"must not {short(describe(spec), 70)}"
    else:
        kw["must_pass_judge"] = True
        what = finding.rule
    body = {"kind": "regression", "task": trace.task, "what": kw, "rule": finding.rule}
    return TestCase(
        id="r_" + digest(body)[:12],
        name=f"regression: {what} ({short(finding.reason, 80)})",
        kind="regression", task=trace.task, cassette=cassette_for(trace),
        max_calls=max(2 * len(trace.steps), len(trace.steps) + 5), source=_source(trace, finding), **kw,
    )


def generate(traces: Iterable[Trace], judge: Optional[Judge] = None, *, golden: bool = True,
             include_reads: bool = False, generalize: bool = True,
             semantic_args: bool = False) -> Tuple[Suite, List[Finding]]:
    """Judge every trace and build a de-duplicated suite. Returns ``(suite, findings)``."""
    judge = judge or HeuristicJudge()
    tests: Dict[str, TestCase] = {}
    findings: List[Finding] = []
    sources = set()
    for trace in traces:
        sources.add(trace.source)
        found = judge.judge(trace)
        findings.extend(found)
        fails = failing(found)
        for f in fails:
            t = regression_test(trace, f, generalize=generalize)
            tests.setdefault(t.id, t)
        if golden and not fails and trace.outcome != "failure":
            t = golden_test(trace, include_reads=include_reads, semantic_args=semantic_args)
            if t is not None:
                tests.setdefault(t.id, t)
    meta = {"generated_at": utcnow(), "generator": f"undolith {__version__}", "judge": judge.name,
            "sources": sorted(sources), "traces": len({f.trace_id for f in findings}),
            "failures_found": sum(1 for f in findings if f.verdict == FAIL)}
    return Suite(list(tests.values()), meta), findings


__all__ = ["call_spec", "cassette_for", "generalize_value", "generate", "golden_test", "regression_test"]
