"""``undolith testgen ...`` subcommands."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, List

from .generate import generate
from .importers import fetch_hf_rows, from_ledger, load_traces
from .judge import make_judge
from .runner import export_pytest, load_agent, run_suite
from .suite import Suite
from .trace import Trace

JUDGE_HELP = "heuristic (default, free, instant) | ollama[:model] | both[:model] (heuristic, then Ollama)"


def add_parser(sub: Any) -> None:
    tg = sub.add_parser("testgen", help="turn agent traces into regression tests")
    ts = tg.add_subparsers(dest="tg_cmd", required=True)

    p = ts.add_parser("from-ledger", help="generate tests from this ledger's sessions")
    p.add_argument("--session", help="only this session")
    p.add_argument("--judge", default="heuristic", help=JUDGE_HELP)
    p.add_argument("--no-golden", action="store_true", help="only regression tests from failures")
    p.add_argument("--merge", action="store_true", help="add to an existing suite instead of replacing it")
    p.add_argument("-o", "--out", default="agent_tests.json")

    p = ts.add_parser("import", help="generate tests from trace files (json/jsonl)")
    p.add_argument("files", nargs="+")
    p.add_argument("--format", default="auto", choices=["auto", "sharegpt", "openai", "anthropic", "trace", "osworld"])
    p.add_argument("--limit", type=int, help="max traces per file")
    p.add_argument("--judge", default="heuristic", help=JUDGE_HELP)
    p.add_argument("--no-golden", action="store_true")
    p.add_argument("--semantic-args", action="store_true", help="golden tests match arguments semantically")
    p.add_argument("--merge", action="store_true")
    p.add_argument("-o", "--out", default="agent_tests.json")

    p = ts.add_parser("judge", help="print judge findings for traces without generating tests")
    p.add_argument("files", nargs="*", help="trace files (omit to judge this ledger)")
    p.add_argument("--format", default="auto")
    p.add_argument("--limit", type=int)
    p.add_argument("--judge", default="heuristic", help=JUDGE_HELP)
    p.add_argument("--json", action="store_true")

    p = ts.add_parser("fetch-hf", help="download rows of a public Hugging Face dataset as jsonl (free, no key)")
    p.add_argument("dataset", help="e.g. zai-org/AgentInstruct")
    p.add_argument("--split", required=True, help="e.g. os, db, kg, alfworld, webshop, mind2web")
    p.add_argument("--config", default="default")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("-o", "--out", required=True)

    p = ts.add_parser("run", help="run a suite against your agent")
    p.add_argument("suite")
    p.add_argument("--agent", required=True, help="module:function taking (task, tools)")
    p.add_argument("--judge", default="heuristic", help=JUDGE_HELP)
    p.add_argument("--verbose", "-v", action="store_true")

    p = ts.add_parser("export-pytest", help="write a pytest file that runs the suite against your agent")
    p.add_argument("suite")
    p.add_argument("--agent", required=True, help="module:function taking (task, tools)")
    p.add_argument("--judge", default="heuristic", help=JUDGE_HELP)
    p.add_argument("-o", "--out", default="tests/test_agent_regressions.py")

    p = ts.add_parser("minimize", help="shrink each test to the smallest cassette that still makes AGENT fail")
    p.add_argument("suite")
    p.add_argument("--agent", required=True, help="the buggy agent the tests should catch (module:function)")
    p.add_argument("--test", action="append", help="only these test ids (repeatable)")
    p.add_argument("--judge", default="heuristic", help=JUDGE_HELP)
    p.add_argument("-o", "--out", help="write the minimised suite here (default: overwrite SUITE)")

    p = ts.add_parser("show", help="list the tests in a suite")
    p.add_argument("suite")


def _traces(files: List[str], fmt: str, limit) -> List[Trace]:
    out: List[Trace] = []
    for f in files:
        out.extend(load_traces(f, fmt, limit=limit))
    return out


def _write(suite: Suite, out: str, merge: bool) -> None:
    path = Path(out)
    if merge and path.exists():
        suite = Suite.load(path).merge(suite)
    suite.save(path)
    kinds = ", ".join(f"{v} {k}" for k, v in sorted(suite.summary().items())) or "no tests"
    print(f"wrote {path}: {kinds} (judge: {suite.meta.get('judge')})")


def run(a: Any) -> int:
    cmd = a.tg_cmd
    if cmd == "fetch-hf":
        rows = fetch_hf_rows(a.dataset, a.split, offset=a.offset, length=a.limit, config=a.config)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"wrote {len(rows)} rows of {a.dataset}/{a.split} to {a.out}")
        return 0
    if cmd == "show":
        suite = Suite.load(a.suite)
        for t in suite.tests:
            print(f"{t.id}  {t.kind:<10}  {t.name}")
        print(f"{len(suite.tests)} tests; meta: {json.dumps(suite.meta, default=str)}")
        return 0
    if cmd == "run":
        report = run_suite(a.suite, load_agent(a.agent), judge=make_judge(a.judge))
        print(report.explain(only_failures=not a.verbose))
        return 0 if report.ok else 1
    if cmd == "minimize":
        from .minimize import minimize_test

        suite, agent, judge = Suite.load(a.suite), load_agent(a.agent), make_judge(a.judge)
        missed = 0
        for i, t in enumerate(suite.tests):
            if a.test and t.id not in a.test:
                continue
            m = minimize_test(t, agent, judge=judge)
            print(m.describe())
            missed += not m.reproduces
            suite.tests[i] = m.test
        suite.save(a.out or a.suite)
        print(f"wrote {a.out or a.suite}" + (f"; {missed} test(s) do not catch this agent" if missed else ""))
        return 0
    if cmd == "export-pytest":
        out = export_pytest(a.suite, a.agent, a.out, judge=a.judge)
        print(f"wrote {out} (+ {out.with_name(out.stem + '.suite.json').name}); run it with: pytest {out}")
        return 0

    judge = make_judge(a.judge)
    if cmd == "judge":
        traces = _traces(a.files, a.format, a.limit) if a.files else from_ledger(a.home)
        findings = [f for t in traces for f in judge.judge(t)]
        if a.json:
            print(json.dumps([f.to_dict() for f in findings], indent=2))
        else:
            for f in findings:
                where = f" step {f.step + 1}" if f.step is not None else ""
                print(f"{f.verdict.upper():<7} {f.trace_id}{where}  [{f.rule}, {f.judge}] {f.reason}")
            print(dict(Counter(f.verdict for f in findings)))
        return 0
    if cmd == "from-ledger":
        traces = from_ledger(a.home, a.session)
        suite, _ = generate(traces, judge, golden=not a.no_golden)
    else:  # import
        traces = _traces(a.files, a.format, a.limit)
        suite, _ = generate(traces, judge, golden=not a.no_golden, semantic_args=a.semantic_args)
    print(f"{len(traces)} trace(s), {suite.meta['failures_found']} failure finding(s)")
    _write(suite, a.out, a.merge)
    return 0
