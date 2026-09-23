import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from undolith import Approval, Policy, Rule, Undolith
from undolith.adapters import FileSystem
from undolith.cli import main
from undolith.testgen import (
    CompositeJudge,
    HeuristicJudge,
    LiveTools,
    OllamaJudge,
    ReplayTools,
    Step,
    Suite,
    Trace,
    UnrecordedCall,
    export_pytest,
    from_anthropic_messages,
    from_ledger,
    from_openai_messages,
    from_record,
    from_sharegpt,
    generate,
    load_traces,
    match_value,
    parse_react_action,
    run_suite,
    run_test,
)

# ------------------------------------------------------------------ fixtures in public dataset shapes (synthetic)
SHAREGPT_OS = {
    "id": "os_demo",
    "conversations": [
        {"from": "human", "loss": None, "value": "You are an assistant that will act like a person..."},
        {"from": "gpt", "loss": False, "value": "Think: demo.\n\nAct: bash\n\n```bash\nls /etc\n```"},
        {"from": "human", "loss": None, "value": "The output of the OS:\n220"},
        {"from": "gpt", "loss": False, "value": "Think: done.\n\nAct: answer(220)"},
        {"from": "human", "loss": None, "value": "Now, I will start a new problem. My problem is:\n\nHow many .log files are there?"},
        {"from": "gpt", "loss": True, "value": "Think: list them.\n\nAct: bash\n\n```bash\nls *.log | wc -l\n```"},
        {"from": "human", "loss": None, "value": "The output of the OS:\n3"},
        {"from": "gpt", "loss": True, "value": "Think: there are 3.\n\nAct: answer(3)"},
    ],
}
SHAREGPT_WEBSHOP = {
    "id": "webshop_demo",
    "conversations": [
        {"from": "human", "loss": None, "value": "You are web shopping..."},
        {"from": "gpt", "loss": False, "value": "Ok."},
        {"from": "human", "loss": None, "value": "WebShop [SEP] Instruction: [SEP] red mug under 10 dollars [SEP] Search"},
        {"from": "gpt", "loss": True, "value": "Thought:\nsearch it.\n\nAction:\nsearch[red mug]"},
        {"from": "human", "loss": None, "value": "[SEP] B01 [SEP] Red Mug [SEP] $8"},
        {"from": "gpt", "loss": True, "value": "Thought:\nthis one.\n\nAction:\nclick[B01]"},
        {"from": "human", "loss": None, "value": "[SEP] Buy Now"},
        {"from": "gpt", "loss": True, "value": "Thought:\nbuy.\n\nAction:\nclick[Buy Now]"},
    ],
}
OPENAI = {"id": "oa", "messages": [
    {"role": "user", "content": "What's the weather in Paris?"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "18C, cloudy"},
    {"role": "assistant", "content": "It's 18C and cloudy in Paris."},
]}
ANTHROPIC = {"id": "an", "messages": [
    {"role": "user", "content": "Delete the temp files"},
    {"role": "assistant", "content": [{"type": "text", "text": "Deleting."},
                                      {"type": "tool_use", "id": "t1", "name": "delete_file", "input": {"path": "tmp/a"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Error: permission denied",
                                  "is_error": True}]},
]}


def test_parse_react_actions():
    assert parse_react_action("Think: x\n\nAct: bash\n\n```bash\nls -l\n```") == ("bash", {"input": "ls -l"}, False, "x")
    assert parse_react_action("Act: answer(220)")[:3] == ("answer", {"input": "220"}, True)
    assert parse_react_action("Thought:\nhm\n\nAction:\nsearch[red mug]")[:2] == ("search", {"input": "red mug"})
    assert parse_react_action("THOUGHT: go.\n ACTION: go to drawer 1")[:2] == ("go", {"input": "to drawer 1"})
    assert parse_react_action("Action: get_relations(Barack Obama)")[:2] == ("get_relations", {"input": "Barack Obama"})
    name, args, final, _ = parse_react_action("Action: Answer\nFinal Answer: [\"x\"]")
    assert name == "answer" and final and args["input"] == '["x"]'
    assert parse_react_action("Ok.") is None


def test_sharegpt_skips_few_shot_demo_and_parses_trajectory():
    t = from_sharegpt(SHAREGPT_OS)
    assert "How many .log files" in t.task
    assert [(s.name, s.args["input"]) for s in t.steps] == [("bash", "ls *.log | wc -l")]
    assert t.steps[0].result.endswith("3") and t.final == "3"


def test_episode_ending_on_an_action_is_its_final():
    t = from_sharegpt(SHAREGPT_WEBSHOP)
    assert [s.name for s in t.steps] == ["search", "click", "click"]
    assert t.final == "click[Buy Now]"
    assert not [f for f in HeuristicJudge().judge(t) if f.verdict != "pass"]


def test_openai_and_anthropic_importers():
    oa = from_record(OPENAI)
    assert oa.task.startswith("What's") and oa.steps[0].args == {"city": "Paris"}
    assert oa.steps[0].result == "18C, cloudy" and oa.final.startswith("It's 18C")
    an = from_record(ANTHROPIC)
    assert an.steps[0].name == "delete_file" and an.steps[0].status == "error" and an.final is None
    assert from_openai_messages(OPENAI["messages"]).steps and from_anthropic_messages(ANTHROPIC["messages"]).steps


def test_load_traces_jsonl_and_hf_rows_shape(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in (SHAREGPT_OS, OPENAI, ANTHROPIC)) + "\n", encoding="utf-8")
    assert [t.id for t in load_traces(p)] == ["os_demo", "oa", "an"]
    hf = tmp_path / "rows.json"
    hf.write_text(json.dumps({"rows": [{"row_idx": 0, "row": SHAREGPT_WEBSHOP}]}), encoding="utf-8")
    assert load_traces(hf)[0].id == "webshop_demo"


# ------------------------------------------------------------------ heuristics
def trace(*steps, final="done", **kw):
    return Trace(id="t", task="do it", steps=[Step(**s) for s in steps], final=final, **kw)


def rules(t, judge=None):
    return {(f.verdict, f.rule) for f in (judge or HeuristicJudge()).judge(t)}


def test_heuristics():
    assert rules(trace({"name": "a"})) == {("pass", "clean")}
    loop = trace(*[{"name": "fetch", "args": {"u": 1}}] * 3)
    assert ("fail", "loop") in rules(loop)
    assert ("fail", "ledger-denied") in rules(trace({"name": "fs.write", "status": "denied"}))
    assert ("fail", "gave-up") in rules(trace({"name": "a"}, final="I'm sorry, I cannot do that."))
    assert ("fail", "ended-on-error") in rules(trace({"name": "a", "status": "error", "error": "boom"}, final=None))
    assert ("warn", "tool-error") in rules(trace({"name": "a", "status": "error", "error": "x"}, {"name": "b"}))
    assert ("fail", "labelled-failure") in rules(trace({"name": "a"}, outcome="failure"))


# ------------------------------------------------------------------ fake Ollama
class _Ollama(BaseHTTPRequestHandler):
    replies = []
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append(body)
        content = json.dumps(self.replies.pop(0))
        data = json.dumps({"message": {"role": "assistant", "content": content}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        data = json.dumps({"models": [{"name": "llama3.2:latest"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def ollama():
    _Ollama.replies, _Ollama.seen = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", _Ollama
    server.shutdown()


def test_ollama_judge_confirmed_and_unconfirmed(ollama):
    host, srv = ollama
    judge = OllamaJudge(host=host)
    assert judge.available()
    t = trace({"name": "bash", "args": {"input": "ls"}}, {"name": "bash", "args": {"input": "grep -c '^...r'"}})
    srv.replies[:] = [{"verdict": "fail", "failing_step": 2, "reason": "wrong column"}, {"confirmed": True}]
    [f] = judge.judge(t)
    assert (f.verdict, f.step, f.rule) == ("fail", 1, "llm")
    assert srv.seen[0]["format"] == "json" and srv.seen[0]["options"]["temperature"] == 0
    assert "CLAIMED FAILURE (at step 2): wrong column" in srv.seen[1]["messages"][1]["content"]
    srv.replies[:] = [{"verdict": "fail", "failing_step": 1, "reason": "no text answer"}, {"confirmed": False}]
    [f] = judge.judge(t)
    assert (f.verdict, f.rule, f.step) == ("warn", "llm-unconfirmed", None)
    srv.replies[:] = [{"verdict": "pass", "reason": "ok"}]
    assert judge.judge(t)[0].verdict == "pass"
    srv.replies[:] = [{"nonsense": 1}]
    assert judge.judge(t)[0].verdict == "unknown"
    srv.replies[:] = [{"equivalent": True}]
    assert judge.equivalent("three", "3", "count logs") is True


def test_ollama_down_is_unknown_not_crash():
    judge = OllamaJudge(host="http://127.0.0.1:9", timeout=2)
    assert not judge.available()
    assert judge.judge(trace({"name": "a"}))[0].verdict == "unknown"
    assert judge.equivalent("a", "b") is None


def test_composite_skips_model_when_heuristics_already_failed(ollama):
    host, srv = ollama
    judge = CompositeJudge(HeuristicJudge(), OllamaJudge(host=host))
    judge.judge(trace(*[{"name": "x"}] * 3))
    assert srv.seen == []  # loop already found: no model call
    srv.replies[:] = [{"verdict": "pass", "reason": "fine"}]
    assert {f.verdict for f in judge.judge(trace({"name": "x"}))} == {"pass"}


# ------------------------------------------------------------------ matchers, replay, generation
def test_matchers():
    assert match_value({"glob": "data/*"}, "data/a.csv")
    assert not match_value({"glob": "data/*"}, "src/a.csv")
    assert match_value({"regex": r"^DROP\b"}, "DROP TABLE x")
    assert match_value("x", "x") and match_value({"any": True}, 5)
    assert match_value({"semantic": "Done."}, "done", HeuristicJudge())
    assert match_value({"semantic": "x"}, "y") is None  # no judge


def test_replay_tools():
    tools = ReplayTools([{"name": "kv.get", "args": {"k": 1}, "result": "a"},
                         {"name": "kv.get", "args": {"k": 1}, "result": "b"}])
    assert [tools.call("kv.get", k=1) for _ in range(3)] == ["a", "b", "b"]
    assert "error" in tools.call("kv.get", k=2)
    assert tools["kv.get"](k=1) == "b"
    with pytest.raises(UnrecordedCall):
        ReplayTools([], on_miss="raise").call("x")
    assert ReplayTools([], on_miss=lambda n, a: f"live {n}").call("x") == "live x"


def test_generate_from_traces_golden_and_regressions():
    good = Trace(id="g", task="count logs", steps=[Step("bash", {"input": "ls *.log | wc -l"}, "3")], final="3")
    loopy = Trace(id="l", task="fetch", steps=[Step("fetch", {"u": 1}, "err")] * 3, final="gave up")
    bad = Trace(id="b", task="clean", steps=[Step("fs.delete", {"path": "data/x.csv"}, status="rejected",
                                                  error="stop")], final=None, outcome="failure")
    suite, findings = generate([good, loopy, bad])
    kinds = sorted((t.kind, t.max_repeats is not None, bool(t.forbid)) for t in suite.tests)
    assert kinds == [("golden", False, False), ("regression", False, True), ("regression", True, False)]
    forbid = next(t for t in suite.tests if t.forbid).forbid[0]
    assert forbid["args"] == {"path": {"glob": "data/*"}}
    again, _ = generate([good, loopy, bad])
    assert sorted(t.id for t in again.tests) == sorted(t.id for t in suite.tests)  # deterministic ids

    def good_agent(task, tools):
        if task == "count logs":
            return tools.call("bash", input="ls *.log | wc -l").strip()
        if task == "fetch":
            tools.call("fetch", u=1)
            return "gave up"
        return "nothing to clean"

    def bad_agent(task, tools):
        if task == "fetch":
            for _ in range(5):
                tools.call("fetch", u=1)
        if task == "clean":
            tools.call("fs.delete", path="data/y.csv")
        return "42"

    assert run_suite(suite, good_agent).ok
    report = run_suite(suite, bad_agent)
    assert report.failed == 3
    text = report.explain()
    assert "forbidden call made" in text and "repeated 5x" in text and "does not match" in text


def test_agent_crash_is_a_failure():
    suite, _ = generate([Trace(id="g", task="t", steps=[Step("a.write", {"x": 1}, "ok")], final="ok")])

    def boom(task, tools):
        raise RuntimeError("model timeout")

    [r] = run_suite(suite, boom).results
    assert not r.passed and "model timeout" in r.explain()


# ------------------------------------------------------------------ the Undolith loop
def run_payroll(tmp_path):
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    (ws / "config.yaml").write_bytes(b"workers: 4\n")
    (ws / "data" / "a.csv").write_bytes(b"1")
    (ws / "data" / "b.csv").write_bytes(b"2")
    policy = Policy(max_destructive_per_session=1)
    policy.add(Rule(verdict="deny", match="fs.*", args={"path": "*.env"}, reason="no secrets"))
    guard = Undolith(tmp_path / "u", policy=policy, approver=lambda a, p: Approval(False, by="ops", note="no"))
    guard.register(FileSystem(ws))

    def buggy(task, tools):
        tools.call("fs.write", path="config.yaml", content=tools.call("fs.read", path="config.yaml") + "x: 1\n")
        tools.call("fs.write", path=".env", content="password=hunter2")
        tools.call("fs.delete", path="data/a.csv")
        tools.call("fs.delete", path="data/b.csv")
        return "done"

    def fixed(task, tools):
        tools.call("fs.write", path="config.yaml", content=tools.call("fs.read", path="config.yaml") + "x: 1\n")
        return "done"

    with guard.session(task="add x to config", agent="v1") as s:
        s.finish(buggy(s.task, LiveTools(s)))
        s.kill("incident")
    (ws / "config.yaml").write_bytes(b"workers: 4\n")
    with guard.session(task="add x to config", agent="v2") as s:
        s.finish(fixed(s.task, LiveTools(s)), outcome="success")
    return guard, buggy, fixed


def test_ledger_traces_label_failures_and_skip_collateral(tmp_path):
    guard, buggy, fixed = run_payroll(tmp_path)
    v1, v2 = from_ledger(guard)
    assert v1.task == "add x to config" and v1.outcome == "failure" and v1.meta["halted"]
    assert [s.status for s in v1.steps] == ["ok", "rolled_back", "denied", "rolled_back", "rejected"]
    assert v1.steps[0].result == "workers: 4\n"  # read results are recorded now
    assert v2.outcome == "success" and v2.final == "done"
    suite, _ = generate([v1, v2])
    assert sorted(t.kind for t in suite.tests) == ["golden", "regression", "regression"]
    assert "hunter2" not in json.dumps(suite.to_dict())  # redacted/identifying-only: secrets stay out
    assert not run_suite(suite, buggy).ok
    assert run_suite(suite, fixed).ok


def test_cli_testgen_roundtrip(tmp_path, monkeypatch, capsys):
    guard, buggy, fixed = run_payroll(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents.py").write_text(
        "def fixed(task, tools):\n"
        "    tools.call('fs.write', path='config.yaml', content=tools.call('fs.read', path='config.yaml') + 'x: 1\\n')\n"
        "    return 'done'\n"
        "def buggy(task, tools):\n"
        "    tools.call('fs.write', path='.env', content='x')\n"
        "    return 'done'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert main(["--home", str(tmp_path / "u"), "testgen", "from-ledger", "-o", "suite.json"]) == 0
    assert "wrote suite.json" in capsys.readouterr().out
    assert main(["testgen", "show", "suite.json"]) == 0
    assert main(["testgen", "run", "suite.json", "--agent", "agents:fixed"]) == 0
    assert main(["testgen", "run", "suite.json", "--agent", "agents:buggy"]) == 1
    out = export_pytest("suite.json", "agents:fixed", tmp_path / "tests" / "test_regressions.py")
    assert out.exists() and (tmp_path / "tests" / "test_regressions.suite.json").exists()
    assert "load_agent('agents:fixed')" in out.read_text()
    (tmp_path / "traces.jsonl").write_text(json.dumps(SHAREGPT_OS) + "\n", encoding="utf-8")
    assert main(["testgen", "import", "traces.jsonl", "-o", "suite.json", "--merge"]) == 0
    assert len(Suite.load("suite.json").tests) == 4
    assert main(["--home", str(tmp_path / "u"), "testgen", "judge"]) == 0
    assert "FAIL" in capsys.readouterr().out


def test_suite_roundtrip(tmp_path):
    suite, _ = generate([from_sharegpt(SHAREGPT_OS)])
    loaded = Suite.load(suite.save(tmp_path / "s.json"))
    assert [t.to_dict() for t in loaded.tests] == [t.to_dict() for t in suite.tests]
    [t] = loaded.tests
    assert run_test(t, lambda task, tools: tools.call("bash", input="ls *.log | wc -l").split(":")[-1].strip()).passed
