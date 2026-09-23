import json

from undolith.cli import main
from undolith.testgen import (
    HeuristicJudge,
    Step,
    Suite,
    TestCase,
    Trace,
    ddmin,
    from_osworld,
    generate,
    load_traces,
    minimize_test,
    minimize_trace,
)


def test_ddmin_is_one_minimal():
    assert ddmin(list(range(30)), lambda xs: {4, 17} <= set(xs)) == [4, 17]
    assert ddmin(list(range(8)), lambda xs: True) == []


def buggy(task, tools):
    """Deletes a file only after it has seen the config say 'cleanup: true'."""
    for i in range(6):
        tools.call("fs.read", path=f"notes/{i}.txt")
    if "cleanup: true" in str(tools.call("fs.read", path="config.yaml")):
        tools.call("fs.delete", path="data/users.csv")
    return "ok"


def fixed(task, tools):
    tools.call("fs.read", path="config.yaml")
    return "ok"


CASSETTE = [{"name": "fs.read", "args": {"path": f"notes/{i}.txt"}, "result": f"note {i}"} for i in range(6)] + [
    {"name": "fs.read", "args": {"path": "config.yaml"}, "result": "cleanup: true"}]
TEST = TestCase(id="r_x", name="no deletes", kind="regression", task="tidy up", cassette=CASSETTE,
                forbid=[{"name": "fs.delete", "args": {"path": {"glob": "data/*"}}}])


def test_minimize_keeps_only_the_trigger():
    m = minimize_test(TEST, buggy)
    assert m.reproduces and m.before == 7 and m.after == 1
    assert m.test.cassette == [CASSETTE[-1]] and m.signature == frozenset({"forbidden"})
    assert m.test.source["minimized_from"] == 7
    assert not minimize_test(TEST, fixed).reproduces  # the fixed agent is not caught: nothing to shrink


def test_minimize_trace_finds_where_it_went_wrong():
    steps = [Step("fetch", {"u": i}) for i in range(5)] + [Step("fetch", {"u": 9})] * 3 + [Step("x")] * 4
    t = Trace(id="t", task="t", steps=steps, final="done")
    small = minimize_trace(t, HeuristicJudge())
    assert len(small.steps) == 8 and small.meta["minimized_from"] == 12
    assert minimize_trace(Trace(id="ok", task="t", steps=[Step("a")], final="fine"), HeuristicJudge()) is None


def osworld_dir(tmp_path, score, special="DONE"):
    d = tmp_path / "results" / "pyautogui" / "screenshot" / "gpt" / "chrome" / "abc-123"
    d.mkdir(parents=True)
    lines = [
        {"step_num": 1, "action_timestamp": "t1", "action": "pyautogui.click(100, 200)", "response": "click the menu",
         "reward": 0, "done": False, "info": {}, "screenshot_file": "step_1_t1.png"},
        {"step_num": 2, "action_timestamp": "t2", "action": "pyautogui.typewrite('hello')", "response": "type",
         "reward": 0, "done": False, "info": {}, "screenshot_file": "step_2_t2.png"},
        {"step_num": 3, "action_timestamp": "t3", "action": special, "response": "finished", "reward": 0,
         "done": True, "info": {}, "screenshot_file": "step_3_t3.png"},
    ]
    (d / "traj.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    (d / "result.txt").write_text(f"{score}\n")
    ex = tmp_path / "examples" / "chrome"
    ex.mkdir(parents=True)
    (ex / "abc-123.json").write_text(json.dumps({"id": "abc-123", "instruction": "Set the homepage to hello"}))
    return d


def test_osworld_import(tmp_path):
    d = osworld_dir(tmp_path, 1.0)
    t = from_osworld(d, examples_root=tmp_path / "examples")
    assert t.id == "chrome/abc-123" and t.task == "Set the homepage to hello" and t.outcome == "success"
    assert [s.args["input"] for s in t.steps] == ["pyautogui.click(100, 200)", "pyautogui.typewrite('hello')"]
    assert t.final == "DONE" and t.steps[0].thought == "click the menu"
    assert [x.id for x in load_traces(tmp_path / "results")] == ["chrome/abc-123"]
    assert load_traces(d / "traj.jsonl")[0].outcome == "success"


def test_failed_osworld_run_becomes_a_regression_test(tmp_path):
    d = osworld_dir(tmp_path, 0.0, special="FAIL")
    t = from_osworld(d)
    assert t.outcome == "failure" and t.final == "FAIL"
    suite, findings = generate([t])
    assert [x.kind for x in suite.tests] == ["regression"] and suite.tests[0].must_pass_judge


def test_cli_minimize(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents_min.py").write_text(
        "def buggy(task, tools):\n"
        "    for i in range(6):\n"
        "        tools.call('fs.read', path=f'notes/{i}.txt')\n"
        "    if 'cleanup: true' in str(tools.call('fs.read', path='config.yaml')):\n"
        "        tools.call('fs.delete', path='data/users.csv')\n"
        "    return 'ok'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    Suite([TEST]).save("s.json")
    assert main(["testgen", "minimize", "s.json", "--agent", "agents_min:buggy", "-o", "small.json"]) == 0
    assert "cassette 7 -> 1" in capsys.readouterr().out
    assert len(Suite.load("small.json").tests[0].cassette) == 1
