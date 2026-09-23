import pytest

from undolith import (
    ActionDenied,
    ActionRejected,
    Approval,
    AutoApprove,
    AutoReject,
    DeviationDetected,
    Held,
    NotReversible,
    Operation,
    Policy,
    Preview,
    Risk,
    Rule,
    SessionHalted,
    UndoConflict,
    Undolith,
    Verdict,
)
from undolith.adapters import FileSystem


def kinds(guard, action_id):
    return [e["kind"] for e in guard.ledger.for_action(action_id)]


def last_action(guard):
    return guard.actions()[-1]["action"]


def test_write_is_simulated_committed_and_undone(guard, workspace):
    (workspace / "a.txt").write_text("old\n")
    with guard.session(agent="t") as s:
        s.call("fs.write", path="a.txt", content="new\n")
    assert (workspace / "a.txt").read_text() == "new\n"
    aid = last_action(guard)
    assert kinds(guard, aid) == ["proposed", "simulated", "prepared", "committed"]
    sim = guard.ledger.for_action(aid)[1]["data"]
    assert "-old" in sim["diff_excerpt"] and "+new" in sim["diff_excerpt"]
    assert guard.undo(aid) is True
    assert (workspace / "a.txt").read_text() == "old\n"
    assert guard.undo(aid) is False  # idempotent
    assert guard.status(aid) == "undone"


def test_undo_of_created_file_removes_it(guard, workspace):
    guard.call("fs.write", path="deep/new.txt", content="x")
    guard.undo(last_action(guard))
    assert not (workspace / "deep" / "new.txt").exists()


def test_delete_move_mkdir_roundtrip(guard, workspace):
    (workspace / "keep.txt").write_bytes(b"\x00\x01binary")
    (workspace / "other.txt").write_text("other")
    with guard.session() as s:
        s.call("fs.mkdir", path="a/b/c")
        s.call("fs.move", src="other.txt", dst="a/b/moved.txt")
        s.call("fs.delete", path="keep.txt")
        s.call("fs.append", path="a/log.txt", content="line\n")
        assert not (workspace / "keep.txt").exists()
        report = s.rollback()
    assert report.ok and len(report.undone) == 4
    assert (workspace / "keep.txt").read_bytes() == b"\x00\x01binary"
    assert (workspace / "other.txt").read_text() == "other"
    assert not (workspace / "a").exists()


def test_reads_are_logged_but_not_simulated(guard, workspace):
    (workspace / "r.txt").write_text("hi")
    assert guard.call("fs.read", path="r.txt") == "hi"
    assert kinds(guard, last_action(guard)) == ["read"]


def test_sandbox_escape_is_refused(guard):
    with pytest.raises(PermissionError):
        guard.call("fs.write", path="../escape.txt", content="x")
    assert guard.status(last_action(guard)) == "failed"


def test_policy_deny_rule_by_argument_glob(guard, workspace):
    guard.policy.add(Rule(verdict="deny", match="fs.*", args={"path": "*.env"}, reason="never touch secrets"))
    with pytest.raises(ActionDenied, match="never touch secrets"):
        guard.call("fs.write", path="prod.env", content="API_KEY=1")
    assert not (workspace / "prod.env").exists()
    assert kinds(guard, last_action(guard)) == ["proposed", "denied"]


def test_blast_radius_escalates_to_approval(tmp_path, workspace):
    for i in range(3):
        (workspace / f"f{i}.txt").write_text(str(i))
    seen = []

    def approver(action, preview):
        seen.append(action.args["path"])
        return Approval(False, by="alice", note="too many deletes")

    guard = Undolith(tmp_path / "u", policy=Policy(max_destructive_per_session=2), approver=approver)
    guard.register(FileSystem(workspace))
    with guard.session() as s:
        s.call("fs.delete", path="f0.txt")
        s.call("fs.delete", path="f1.txt")
        with pytest.raises(ActionRejected, match="too many deletes"):
            s.call("fs.delete", path="f2.txt")
    assert seen == ["f2.txt"]
    assert (workspace / "f2.txt").exists()
    proposed = guard.ledger.for_action(last_action(guard))[0]["data"]
    assert proposed["verdict"] == "approve" and "blast radius" in proposed["reason"]


def test_irreversible_is_held_then_released(tmp_path):
    sent = []
    guard = Undolith(tmp_path / "u")
    guard.register(Operation("mail", "send", run=lambda to, body: sent.append(to) or {"sent": True},
                             risk=Risk.IRREVERSIBLE,
                             simulate=lambda to, body: Preview(f"send to {to}", predicted={"sent": True}),
                             observe=lambda args, r: {"sent": r["sent"]}))
    held = guard.call("mail.send", to="bob@example.com", body="hi")
    assert isinstance(held, Held) and sent == []
    assert [h["action"] for h in guard.held()] == [held.action_id]
    assert guard.release(held.action_id, by="alice") == {"sent": True}
    assert sent == ["bob@example.com"] and guard.held() == []
    proof = guard.verify(held.action_id)
    assert proof["authorized"] and proof["authorization"]["by"] == "alice"


def test_approver_can_defer_to_outbox(tmp_path):
    guard = Undolith(tmp_path / "u", approver=lambda action, preview: None)
    guard.register(Operation("mail", "send", run=lambda: {"sent": True}, risk="irreversible"))
    held = guard.call("mail.send")
    assert isinstance(held, Held) and "deferred" in held.reason
    assert guard.release(held.action_id) == {"sent": True}


def test_discard_held(tmp_path):
    guard = Undolith(tmp_path / "u")
    guard.register(Operation("mail", "send", run=lambda: None, risk="irreversible"))
    held = guard.call("mail.send")
    guard.discard(held.action_id, reason="spam")
    assert guard.status(held.action_id) == "discarded"
    assert not guard.verify(held.action_id)["authorized"]


def test_destructive_without_inverse_needs_approval(tmp_path):
    guard = Undolith(tmp_path / "u", approver=AutoApprove("bot"))
    calls = []
    guard.register(Operation("db", "drop_table", run=lambda name: calls.append(name), risk="destructive"))
    guard.call("db.drop_table", name="users")
    aid = last_action(guard)
    assert kinds(guard, aid) == ["proposed", "simulated", "approved", "prepared", "committed"]
    with pytest.raises(NotReversible):
        guard.undo(aid)


def test_undo_conflict_when_state_drifted(guard, workspace):
    guard.call("fs.write", path="c.txt", content="agent")
    aid = last_action(guard)
    (workspace / "c.txt").write_text("human edit")
    with pytest.raises(UndoConflict):
        guard.undo(aid)
    assert (workspace / "c.txt").read_text() == "human edit"
    guard.undo(aid, force=True)
    assert not (workspace / "c.txt").exists()


def test_rollback_newest_first_handles_same_file(guard, workspace):
    (workspace / "s.txt").write_text("v0")
    with guard.session() as s:
        for v in ("v1", "v2", "v3"):
            s.call("fs.write", path="s.txt", content=v)
        assert s.rollback().ok
    assert (workspace / "s.txt").read_text() == "v0"


def test_batch_is_all_or_nothing(guard, workspace):
    with guard.session() as s:
        s.call("fs.write", path="before.txt", content="kept")
        with pytest.raises(RuntimeError):
            with s.batch():
                s.call("fs.write", path="one.txt", content="1")
                s.call("fs.write", path="two.txt", content="2")
                raise RuntimeError("agent crashed mid-plan")
    assert (workspace / "before.txt").exists()
    assert not (workspace / "one.txt").exists() and not (workspace / "two.txt").exists()


def test_deviation_triggers_auto_rollback(tmp_path):
    state = {"value": 0}

    def run(n):
        state["value"] += n * 2  # buggy tool: does double what it claims

    guard = Undolith(tmp_path / "u")
    guard.register(Operation(
        "counter", "add", run=run, risk="write",
        simulate=lambda n: Preview(f"add {n}", predicted={"value": state["value"] + n}),
        snapshot=lambda n: {"value": state["value"]},
        undo=lambda args, snap, result: state.update(value=snap["value"]),
        observe=lambda args, result: {"value": state["value"]},
    ))
    with pytest.raises(DeviationDetected) as exc:
        guard.call("counter.add", n=5)
    assert exc.value.mismatches == {"value": {"predicted": 5, "observed": 10}}
    assert state["value"] == 0
    assert kinds(guard, exc.value.action_id)[-2:] == ["deviation", "undone"]


def test_deviation_warn_only(tmp_path):
    guard = Undolith(tmp_path / "u", policy=Policy(on_deviation="warn"))
    guard.register(Operation("x", "do", run=lambda: 2, simulate=lambda: Preview("", predicted={"r": 1}),
                             observe=lambda a, r: {"r": r}))
    assert guard.call("x.do") == 2
    assert "deviation" in kinds(guard, last_action(guard))


def test_session_kill_switch_rolls_back_and_blocks(guard, workspace):
    s = guard.session()
    s.call("fs.write", path="k.txt", content="x")
    report = s.kill("agent went rogue")
    assert report.undone and not (workspace / "k.txt").exists()
    with pytest.raises(SessionHalted):
        s.call("fs.write", path="k.txt", content="again")
    guard.resume(s.id)
    s.call("fs.write", path="k.txt", content="ok")


def test_global_kill_switch(guard):
    guard.kill(reason="stop everything")
    with pytest.raises(SessionHalted, match="global"):
        guard.call("fs.write", path="g.txt", content="x")
    guard.resume()
    guard.call("fs.write", path="g.txt", content="x")


def test_strict_policy_holds_without_approver(tmp_path, workspace):
    guard = Undolith(tmp_path / "u", policy=Policy.strict())
    guard.register(FileSystem(workspace))
    held = guard.call("fs.write", path="h.txt", content="x")
    assert isinstance(held, Held) and not (workspace / "h.txt").exists()
    guard.release(held.action_id)
    assert (workspace / "h.txt").read_text() == "x"


def test_approver_rejection(tmp_path, workspace):
    guard = Undolith(tmp_path / "u", policy=Policy.strict(), approver=AutoReject(note="nope"))
    guard.register(FileSystem(workspace))
    with pytest.raises(ActionRejected):
        guard.call("fs.write", path="h.txt", content="x")


def test_redaction_keeps_secrets_out_of_ledger(tmp_path):
    guard = Undolith(tmp_path / "u")
    guard.register(Operation("api", "login", run=lambda user, password: True, risk="write"))
    guard.call("api.login", user="bob", password="hunter2")
    raw = (tmp_path / "u" / "ledger.sqlite3").read_bytes()
    assert b"hunter2" not in raw
    assert guard.ledger.for_action(last_action(guard))[0]["data"]["args"]["password"].startswith("[redacted")


def test_policy_roundtrip_and_fingerprint():
    p = Policy.from_dict({
        "defaults": {"write": "approve"},
        "rules": [{"match": "fs.write", "args": {"path": "*.env"}, "verdict": "deny"}],
        "max_destructive_per_session": 3,
        "deviation": {"threshold": 0.5, "on": "halt"},
    })
    assert p.defaults[Risk.WRITE] is Verdict.APPROVE and p.defaults[Risk.READ] is Verdict.ALLOW
    q = Policy.from_dict(p.to_dict())
    assert q.fingerprint == p.fingerprint and q.on_deviation == "halt"
    with pytest.raises(ValueError):
        Policy(on_deviation="explode")


def test_ledger_verifies_after_a_busy_session(guard, workspace):
    with guard.session() as s:
        for i in range(10):
            s.call("fs.write", path=f"n{i}.txt", content=str(i))
        s.rollback()
    report = guard.verify_chain()
    assert report.ok, report.problems


def test_jsonl_and_hmac_backends(tmp_path, workspace):
    guard = Undolith(tmp_path / "u", store="jsonl", signer="hmac").register(FileSystem(workspace))
    guard.call("fs.write", path="j.txt", content="x")
    guard.undo(last_action(guard))
    assert guard.verify_chain().ok
    assert (tmp_path / "u" / "ledger.jsonl").exists()


def test_home_is_gitignored(guard, tmp_path):
    assert (tmp_path / ".undolith" / ".gitignore").read_text() == "*\n"
