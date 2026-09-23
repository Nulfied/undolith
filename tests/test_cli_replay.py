import json

from undolith import Policy, Undolith
from undolith.adapters import FileSystem
from undolith.cli import main


def test_cli_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    guard = Undolith(".undolith").register(FileSystem("."))
    with guard.session(id="ses_demo") as s:
        s.call("fs.write", path="a.txt", content="A")
        s.call("fs.write", path="b.txt", content="B")
    first = guard.actions()[0]["action"]

    assert main(["log"]) == 0
    assert "fs.write" in capsys.readouterr().out
    assert main(["verify-chain"]) == 0
    assert "OK" in capsys.readouterr().out

    assert main(["proof", first, "--segment", "-o", "proof.json"]) == 0
    capsys.readouterr()
    assert main(["pubkey"]) == 0
    pub = capsys.readouterr().out.strip()
    assert main(["verify-proof", "proof.json", "--pubkey", pub]) == 0
    assert "VALID" in capsys.readouterr().out

    assert main(["undo", first]) == 0
    assert not (tmp_path / "a.txt").exists()
    assert main(["rollback", "ses_demo"]) == 0
    assert not (tmp_path / "b.txt").exists()

    assert main(["kill"]) == 0
    assert (tmp_path / ".undolith" / "KILL").exists()
    assert main(["resume"]) == 0
    assert main(["sessions"]) == 0
    assert "ses_demo" in capsys.readouterr().out


def test_cli_verify_proof_rejects_tampering(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    guard = Undolith(".undolith").register(FileSystem("."))
    guard.call("fs.write", path="a.txt", content="A")
    main(["proof", guard.actions()[0]["action"], "-o", "p.json"])
    proof = json.loads((tmp_path / "p.json").read_text())
    proof["entries"][0]["data"]["args"]["content"] = "Z"
    (tmp_path / "p.json").write_text(json.dumps(proof))
    assert main(["verify-proof", "p.json"]) == 1


def test_cli_policy_prints_json(capsys):
    assert main(["policy"]) == 0
    assert json.loads(capsys.readouterr().out)["defaults"]["irreversible"] == "approve"


def test_replay_into_sandbox_reproduces_final_state(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    guard = Undolith(tmp_path / "u").register(FileSystem(live))
    with guard.session(id="run1") as s:
        s.call("fs.write", path="plan.md", content="step 1\n")
        s.call("fs.append", path="plan.md", content="step 2\n")
        s.call("fs.write", path="scratch.txt", content="tmp")
        guard.undo(guard.actions()[-1]["action"])
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    into = Undolith(tmp_path / "r", policy=Policy.permissive()).register(FileSystem(sandbox))
    steps = guard.replay("run1", into)
    assert [s.match for s in steps] == [True, True, True]
    assert steps[-1].undone
    assert (sandbox / "plan.md").read_text() == "step 1\nstep 2\n"
    assert not (sandbox / "scratch.txt").exists()
