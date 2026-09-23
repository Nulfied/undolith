"""``undolith`` command line: inspect, verify, undo and kill from outside the agent.

Commands that execute inverses or held actions (undo, rollback, release, replay)
need the same adapters the agent registered. Point ``--app module:attr`` at your
``Undolith`` instance (or a zero-argument factory). Without ``--app`` the
built-in filesystem adapter rooted at the current directory is used.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from . import __version__
from .adapters import FileSystem
from .core import Undolith
from .policy import Policy
from .proof import verify_proof


def _load_app(spec: Optional[str], home: str) -> Undolith:
    if not spec:
        return Undolith(home).register(FileSystem("."))
    module_name, _, attr = spec.partition(":")
    sys.path.insert(0, str(Path.cwd()))
    obj: Any = importlib.import_module(module_name)
    for part in (attr or "guard").split("."):
        obj = getattr(obj, part)
    if callable(obj) and not isinstance(obj, Undolith):
        obj = obj()
    if not isinstance(obj, Undolith):
        raise SystemExit(f"{spec} is not an Undolith instance")
    return obj


def _print_rows(rows: List[dict], cols: List[str]) -> None:
    if not rows:
        print("(nothing)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.upper().ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def _dump(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="undolith", description="Verifiable side-effects and rollback for AI agents.")
    ap.add_argument("--home", default=".undolith", help="ledger directory (default: .undolith)")
    ap.add_argument("--app", help="module:attr of your Undolith instance (needed to run your adapters' inverses)")
    ap.add_argument("--version", action="version", version=f"undolith {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("log", help="list actions")
    p.add_argument("--session")
    p.add_argument("--json", action="store_true")
    p.add_argument("--entries", action="store_true", help="raw ledger entries instead of one row per action")

    p = sub.add_parser("show", help="all ledger entries for one action")
    p.add_argument("action")

    sub.add_parser("sessions", help="list sessions")
    sub.add_parser("verify-chain", help="check every hash, link, signature and blob")

    p = sub.add_parser("proof", help="export a signed proof for an action")
    p.add_argument("action")
    p.add_argument("--segment", action="store_true", help="include the chain segment up to the head")
    p.add_argument("-o", "--out")

    p = sub.add_parser("verify-proof", help="check a proof file offline")
    p.add_argument("file")
    p.add_argument("--pubkey", help="hex Ed25519 public key you trust (pins the signer)")

    sub.add_parser("pubkey", help="print this ledger's public key")

    p = sub.add_parser("undo", help="undo one action")
    p.add_argument("action")
    p.add_argument("--force", action="store_true", help="undo even if state drifted since commit")

    p = sub.add_parser("rollback", help="undo a whole session, newest first")
    p.add_argument("session")
    p.add_argument("--to", type=int, default=0, help="only actions after this ledger seq")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("kill", help="kill switch: halt a session (and roll it back) or everything")
    p.add_argument("--session")
    p.add_argument("--reason", default="manual kill switch")
    p.add_argument("--no-rollback", action="store_true")

    p = sub.add_parser("resume", help="lift a kill switch")
    p.add_argument("--session")

    sub.add_parser("held", help="list actions waiting for approval")
    p = sub.add_parser("release", help="approve and commit a held action")
    p.add_argument("action")
    p.add_argument("--by", default="cli")
    p = sub.add_parser("discard", help="drop a held action")
    p.add_argument("action")
    p.add_argument("--by", default="cli")
    p.add_argument("--reason", default="")

    p = sub.add_parser("replay", help="re-run a session in a sandbox directory and compare results")
    p.add_argument("session")
    p.add_argument("--sandbox", required=True, help="empty directory to replay filesystem actions into")

    p = sub.add_parser("policy", help="print the default policy as JSON (a starting point for your own)")

    p = sub.add_parser("ui", help="local web console: ledger, outbox, undo, kill switch")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")

    from .testgen import cli as testgen_cli

    testgen_cli.add_parser(sub)

    a = ap.parse_args(argv)

    if a.cmd == "policy":
        _dump(Policy().to_dict())
        return 0
    if a.cmd == "testgen":
        return testgen_cli.run(a)

    guard = _load_app(a.app, a.home)

    if a.cmd == "ui":
        from .ui import serve

        serve(guard, a.host, a.port, open_browser=not a.no_browser)
        return 0

    if a.cmd == "log":
        if a.entries:
            rows = guard.ledger.entries(session=a.session)
            if a.json:
                _dump(rows)
            else:
                _print_rows([{**r, "summary": (r.get("data") or {}).get("summary")
                              or (r.get("data") or {}).get("reason") or ""} for r in rows],
                            ["seq", "ts", "kind", "action", "tool", "op", "summary"])
            return 0
        rows = guard.actions(session=a.session)
        if a.json:
            _dump(rows)
        else:
            _print_rows(rows, ["action", "ts", "session", "qualname", "risk", "status", "authorized"])
        return 0
    if a.cmd == "show":
        _dump(guard.ledger.for_action(a.action))
        return 0
    if a.cmd == "sessions":
        seen = {}
        for r in guard.actions():
            s = seen.setdefault(r["session"], {"session": r["session"], "agent": r["agent"], "first": r["ts"],
                                               "actions": 0, "halted": guard.is_halted(r["session"])})
            s["actions"] += 1
        _print_rows(list(seen.values()), ["session", "agent", "first", "actions", "halted"])
        return 0
    if a.cmd == "verify-chain":
        report = guard.verify_chain()
        if report.ok:
            print(f"OK: {report.length} entries, head {report.head}")
            return 0
        print(f"BROKEN: {len(report.problems)} problem(s) in {report.length} entries")
        for pr in report.problems:
            print(f"  seq {pr['seq']}: {pr['problem']}")
        return 1
    if a.cmd == "proof":
        proof = guard.verify(a.action, segment=a.segment)
        text = json.dumps(proof, indent=2, sort_keys=True)
        if a.out:
            Path(a.out).write_text(text + "\n", encoding="utf-8")
            print(f"wrote {a.out}: {proof['qualname']} status={proof['status']} authorized={proof['authorized']}")
        else:
            print(text)
        return 0
    if a.cmd == "verify-proof":
        proof = json.loads(Path(a.file).read_text(encoding="utf-8"))
        check = verify_proof(proof, public_key=a.pubkey)
        print(("VALID" if check.valid else "INVALID") +
              f": status={check.status} happened={check.happened} authorized={check.authorized}")
        for e in check.errors:
            print(f"  error: {e}")
        for w in check.warnings:
            print(f"  warning: {w}")
        return 0 if check.valid else 1
    if a.cmd == "pubkey":
        pk = guard.signer.public_key
        print(pk.hex() if pk else f"{guard.signer.algorithm} ledger has no public key")
        return 0
    if a.cmd == "undo":
        done = guard.undo(a.action, force=a.force, by="cli")
        print("undone" if done else "already undone")
        return 0
    if a.cmd == "rollback":
        rep = guard.rollback(a.session, to=a.to, force=a.force, by="cli")
        print(f"undone {len(rep.undone)}, skipped {len(rep.skipped)} (irreversible), failed {len(rep.failed)}")
        for s in rep.skipped:
            print(f"  skipped {s['action']}: {s['reason']}")
        for f in rep.failed:
            print(f"  FAILED  {f['action']}: {f['error']}")
        return 0 if not rep.failed else 1
    if a.cmd == "kill":
        rep = guard.kill(a.session, reason=a.reason, rollback=not a.no_rollback, by="cli")
        if a.session is None:
            print(f"global kill switch engaged: {guard.kill_file}")
        else:
            print(f"session {a.session} halted" + (f"; rolled back {len(rep.undone)} action(s)" if rep else ""))
        return 0
    if a.cmd == "resume":
        guard.resume(a.session, by="cli")
        print("resumed")
        return 0
    if a.cmd == "held":
        _print_rows(guard.held(), ["action", "ts", "qualname", "agent", "summary", "reason"])
        return 0
    if a.cmd == "release":
        result = guard.release(a.action, by=a.by)
        print("released:", json.dumps(result, default=str)[:500])
        return 0
    if a.cmd == "discard":
        guard.discard(a.action, by=a.by, reason=a.reason)
        print("discarded")
        return 0
    if a.cmd == "replay":
        sandbox = Path(a.sandbox)
        sandbox.mkdir(parents=True, exist_ok=True)
        into = Undolith(sandbox / ".undolith-replay", policy=Policy.permissive()).register(FileSystem(sandbox))
        steps = guard.replay(a.session, into)
        _print_rows([{"action": s.action, "qualname": s.qualname, "match": s.match, "undone": s.undone,
                      "error": s.error or ""} for s in steps], ["action", "qualname", "match", "undone", "error"])
        return 0 if all(s.match for s in steps) else 1
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
