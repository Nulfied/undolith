"""Agent traces → regression tests.

    from undolith.testgen import from_ledger, generate, run_suite

    suite, findings = generate(from_ledger(".undolith"))     # failures are already labelled by the ledger
    suite.save("agent_tests.json")
    report = run_suite(suite, my_agent)                      # replays recorded tool responses; no side effects
    assert report.ok, report.explain()

Traces can also come from OpenAI/Anthropic message logs or public ShareGPT/ReAct
datasets (e.g. AgentInstruct), judged by heuristics and/or a local Ollama model.
"""

from .generate import generate, golden_test, regression_test
from .importers import (
    fetch_hf_rows,
    from_anthropic_messages,
    from_ledger,
    from_openai_messages,
    from_record,
    from_osworld,
    from_sharegpt,
    load_osworld,
    load_traces,
    parse_react_action,
)
from .minimize import Minimized, ddmin, minimize_test, minimize_trace
from .judge import CompositeJudge, Finding, HeuristicJudge, Judge, OllamaJudge, make_judge
from .runner import LiveTools, ReplayTools, SuiteReport, TestResult, UnrecordedCall, export_pytest, load_agent, run_suite, run_test
from .suite import Suite, TestCase, load_suite, match_call, match_value
from .trace import Step, Trace

__all__ = [
    "CompositeJudge", "Finding", "HeuristicJudge", "Judge", "LiveTools", "OllamaJudge", "ReplayTools", "Step", "Suite",
    "SuiteReport", "TestCase", "TestResult", "Trace", "UnrecordedCall", "export_pytest", "fetch_hf_rows",
    "from_anthropic_messages", "from_ledger", "from_osworld", "load_osworld", "Minimized", "ddmin",
    "minimize_test", "minimize_trace", "from_openai_messages", "from_record", "from_sharegpt", "generate",
    "golden_test", "load_agent", "load_suite", "load_traces", "make_judge", "match_call", "match_value",
    "parse_react_action", "regression_test", "run_suite", "run_test",
]
