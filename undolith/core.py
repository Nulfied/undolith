"""The Undolith gateway: every guarded tool call flows through here.

    classify ─> policy ─> simulate ─> approve/hold ─> snapshot ─> commit ─> observe ─> compare
                   │                                                                    │
                  deny                                                  deviation ─> rollback / halt
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Union

from ._canon import digest, new_id, short, utcnow
from .classify import classify_name
from .ledger import ChainReport, Ledger
from .lifecycle import UNDOABLE, first, last, summarize
from .model import (
    Action,
    ActionDenied,
    ActionRejected,
    Approval,
    DeviationDetected,
    Held,
    NotReversible,
    Preview,
    Risk,
    SessionHalted,
    UndoConflict,
    UndolithError,
    UnknownAction,
    Verdict,
)
from .ops import Operation, Registry, _loop, resolve
from .policy import Policy
from .signing import Signer, load_signer
from .store import BlobStore, Store, open_store

_current: ContextVar[Optional["Session"]] = ContextVar("undolith_session", default=None)

Approver = Callable[[Action, Optional[Preview]], Union[bool, Approval]]


@dataclass
class SessionStats:
    actions: int = 0
    mutations: int = 0
    destructive: int = 0


@dataclass
class RollbackReport:
    session: str
    undone: List[str] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)  # not reversible
    failed: List[Dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.skipped


@dataclass
class ReplayStep:
    action: str
    qualname: str
    original_sha256: Optional[str]
    replay_sha256: Optional[str] = None
    undone: bool = False
    error: Optional[str] = None

    @property
    def match(self) -> bool:
        return self.error is None and self.original_sha256 == self.replay_sha256


class Session:
    """A run of one agent. Tracks blast-radius counters and scopes rollback."""

    def __init__(self, guard: "Undolith", id: Optional[str] = None, agent: Optional[str] = None):
        self.guard = guard
        self.id = id or new_id("ses")
        self.agent = agent or guard.agent
        self.stats = SessionStats()
        self._tokens: list = []

    def call(self, name: str, /, **args: Any) -> Any:
        return self.guard._execute(self, name, args)

    async def acall(self, name: str, /, **args: Any) -> Any:
        return await self.guard._aexecute(self, name, args)

    def checkpoint(self) -> int:
        """A ledger position; ``rollback(to=checkpoint)`` undoes everything after it."""
        head = self.guard.ledger.head()
        return head["seq"] if head else 0

    def rollback(self, to: int = 0, *, force: bool = False) -> RollbackReport:
        return self.guard.rollback(self.id, to=to, force=force)

    @contextmanager
    def batch(self) -> Iterator["Session"]:
        """All-or-nothing: if the block raises, everything it committed is undone."""
        cp = self.checkpoint()
        try:
            yield self
        except BaseException:
            self.rollback(to=cp)
            raise

    def kill(self, reason: str = "", *, rollback: bool = True) -> Optional[RollbackReport]:
        return self.guard.kill(self.id, reason=reason, rollback=rollback)

    def __enter__(self) -> "Session":
        self._tokens.append(_current.set(self))
        return self

    def __exit__(self, *exc) -> bool:
        _current.reset(self._tokens.pop())
        return False

    def __repr__(self) -> str:
        return f"<Session {self.id} agent={self.agent}>"


class Undolith:
    """Simulate-first, signed, reversible execution for agent tool calls.

    >>> guard = Undolith(".undolith")
    >>> guard.register(FileSystem("workspace"))
    >>> with guard.session(agent="planner") as s:
    ...     s.call("fs.write", path="notes.md", content="hello")
    ...     s.rollback()
    """

    def __init__(
        self,
        home: Union[str, Path] = ".undolith",
        *,
        policy: Optional[Policy] = None,
        approver: Optional[Approver] = None,
        store: Union[str, Store] = "sqlite",
        signer: Union[str, Signer] = "ed25519",
        agent: str = "agent",
    ):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        ignore = self.home / ".gitignore"
        if not ignore.exists():  # keys, args and snapshots must never be committed by accident
            ignore.write_text("*\n")
        self.blobs = BlobStore(self.home / "blobs")
        store_obj = open_store(self.home, store) if isinstance(store, str) else store
        signer_obj = load_signer(self.home / "keys", signer) if isinstance(signer, str) else signer
        self.ledger = Ledger(store_obj, signer_obj)
        self.policy = policy or Policy()
        self.approver = approver
        self.registry = Registry()
        self.agent = agent
        self._default: Optional[Session] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ registration
    def register(self, *items: Any) -> "Undolith":
        self.registry.add(*items)
        return self

    def tool(self, name: Optional[str] = None, *, risk: Any = None, simulate=None, snapshot=None,
             undo=None, observe=None, reversible=None, description: str = ""):
        """Decorator: guard any function. ``name`` may be ``"tool.op"`` or just ``"op"``.

        The returned wrapper has ``.simulator``, ``.snapshotter``, ``.inverse`` and
        ``.observer`` decorators for attaching hooks after the fact.
        """

        def deco(fn):
            qual = name or fn.__name__
            tool, _, op_name = qual.rpartition(".")
            op = Operation(
                tool=tool or "fn", name=op_name, run=fn,
                risk=risk if risk is not None else classify_name(op_name, description or (fn.__doc__ or "")),
                simulate=simulate, snapshot=snapshot, undo=undo, observe=observe, reversible=reversible,
                description=description or (fn.__doc__ or "").strip(),
            )
            self.register(op)
            sig = inspect.signature(fn)

            def bind(a, kw):
                bound = sig.bind(*a, **kw)
                bound.apply_defaults()
                return dict(bound.arguments)

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def wrapper(*a, **kw):
                    return await self.acall(op.qualname, **bind(a, kw))
            else:
                @functools.wraps(fn)
                def wrapper(*a, **kw):
                    return self.call(op.qualname, **bind(a, kw))

            def hook(attr):
                def setter(f):
                    setattr(op, attr, f)
                    return f
                return setter

            wrapper.operation = op
            wrapper.simulator = hook("simulate")
            wrapper.snapshotter = hook("snapshot")
            wrapper.inverse = hook("undo")
            wrapper.observer = hook("observe")
            return wrapper

        return deco

    # ------------------------------------------------------------------ sessions & calls
    def session(self, id: Optional[str] = None, *, agent: Optional[str] = None) -> Session:
        return Session(self, id=id, agent=agent)

    @property
    def current(self) -> Session:
        s = _current.get()
        if s is not None and s.guard is self:
            return s
        with self._lock:
            if self._default is None:
                self._default = Session(self)
            return self._default

    def call(self, name: str, /, **args: Any) -> Any:
        """Run a registered operation through the full pipeline in the current session."""
        return self._execute(self.current, name, args)

    async def acall(self, name: str, /, **args: Any) -> Any:
        return await self._aexecute(self.current, name, args)

    async def _aexecute(self, session: Session, name: str, args: Dict[str, Any]) -> Any:
        token = _loop.set(asyncio.get_running_loop())
        try:
            return await asyncio.to_thread(self._execute, session, name, args)
        finally:
            _loop.reset(token)

    # ------------------------------------------------------------------ the pipeline
    def _ids(self, action: Action) -> Dict[str, Any]:
        return {"action": action.id, "session": action.session, "agent": action.agent,
                "tool": action.tool, "op": action.op}

    def _execute(self, session: Session, name: str, args: Dict[str, Any]) -> Any:
        op = self.registry.get(name)
        args = dict(args)
        risk = op.risk_for(args)
        action = Action(new_id("act"), session.id, session.agent, op.tool, op.name, args, risk)
        ids = self._ids(action)
        self._ensure_running(session.id)
        reversible = op.is_reversible(args)
        verdict, reason = self.policy.decide(action, session.stats, reversible=reversible,
                                             has_simulator=op.simulate is not None)
        session.stats.actions += 1

        if risk is Risk.READ and verdict is Verdict.ALLOW:
            result = resolve(op.run(**args))
            if self.policy.log_reads:
                self.ledger.append("read", **ids, data={"args": self._redact(args), "result_sha256": digest(result)})
            return result

        self.ledger.append("proposed", **ids, data={
            "risk": risk.label, "verdict": verdict.value, "reason": reason, "reversible": reversible,
            "args": self._redact(args), "args_ref": self.blobs.put_json(args), "policy": self.policy.fingerprint,
        })
        if verdict is Verdict.DENY:
            self.ledger.append("denied", **ids, data={"reason": reason})
            raise ActionDenied(action.id, reason)

        preview = None
        if verdict is not Verdict.ALLOW:
            preview = self._simulate(op, args)
            self.ledger.append("simulated", **ids, data=self._preview_data(preview))
            if verdict is Verdict.SIMULATE and not preview.simulated and self.policy.require_simulator:
                verdict, reason = Verdict.APPROVE, f"{reason}; simulation could not predict the effects"
                self.ledger.append("escalated", **ids, data={"verdict": verdict.value, "reason": reason})

        decision = None
        if verdict is Verdict.APPROVE and self.approver is not None:
            decision = resolve(self.approver(action, preview))  # None means "not now": park it in the outbox
        if decision is not None:
            approval = _as_approval(decision)
            self.ledger.append("approved" if approval.approved else "rejected", **ids,
                               data={"by": approval.by, "note": approval.note})
            if not approval.approved:
                raise ActionRejected(action.id, approval.note or "rejected by approver")
        elif verdict in (Verdict.APPROVE, Verdict.HOLD):
            why = reason if verdict is Verdict.HOLD else (
                f"{reason}; awaiting approval ({'deferred by approver' if self.approver else 'no approver configured'})")
            self.ledger.append("held", **ids, data={"reason": why})
            return Held(action.id, op.qualname, preview, why)

        return self._commit(session, op, action, preview)

    def _simulate(self, op: Operation, args: Dict[str, Any]) -> Preview:
        if op.simulate is None:
            return Preview(summary=f"{op.qualname}({short(args, 120)}): no simulator, effects unknown", simulated=False)
        try:
            p = resolve(op.simulate(**args))
        except Exception as exc:
            return Preview(summary=f"simulation failed: {type(exc).__name__}: {exc}", simulated=False,
                           details={"error": repr(exc)})
        return p if isinstance(p, Preview) else Preview(summary=str(p))

    def _preview_data(self, p: Preview) -> Dict[str, Any]:
        data = {"summary": p.summary, "predicted": p.predicted, "simulated": p.simulated, "details": p.details}
        if p.diff:
            data["diff_ref"] = self.blobs.put(p.diff.encode("utf-8"))
            data["diff_excerpt"] = p.diff[:2000]
        return data

    def _commit(self, session: Session, op: Operation, action: Action, preview: Optional[Preview]) -> Any:
        ids, args = self._ids(action), action.args
        try:
            snap_ref = self.blobs.put_json(resolve(op.snapshot(**args))) if op.snapshot is not None else None
        except Exception as exc:  # no snapshot means no safe undo: refuse to run
            self.ledger.append("failed", **ids, data={"error": f"snapshot failed: {type(exc).__name__}: {exc}"})
            raise
        # Write-ahead: the snapshot is durable before the side effect happens, so a crash mid-call is recoverable.
        self.ledger.append("prepared", **ids, data={"snapshot_ref": snap_ref})
        try:
            result = resolve(op.run(**args))
        except Exception as exc:
            self.ledger.append("failed", **ids, data={"error": f"{type(exc).__name__}: {exc}"})
            raise
        observed = self._observe(op, args, result)
        self.ledger.append("committed", **ids, data={
            "result_ref": self.blobs.put_json(result), "result_sha256": digest(result),
            "result_preview": short(result), "observed": observed,
        })
        if action.risk >= Risk.WRITE:
            session.stats.mutations += 1
        if action.risk >= Risk.DESTRUCTIVE:
            session.stats.destructive += 1
        if preview is not None and preview.predicted and observed is not None:
            self._check_deviation(session, action, preview.predicted, observed)
        return result

    def _observe(self, op: Operation, args: Dict[str, Any], result: Any) -> Optional[Dict[str, Any]]:
        if op.observe is None:
            return None
        try:
            return resolve(op.observe(args, result))
        except Exception as exc:
            return {"$error": f"{type(exc).__name__}: {exc}"}

    def _check_deviation(self, session: Session, action: Action, predicted: Dict[str, Any],
                         observed: Dict[str, Any]) -> None:
        compared = [k for k in predicted if k in observed]
        if not compared:
            return
        mismatches = {k: {"predicted": predicted[k], "observed": observed[k]}
                      for k in compared if _norm(predicted[k]) != _norm(observed[k])}
        score = len(mismatches) / len(compared)
        if score <= self.policy.deviation_threshold:
            return
        response = self.policy.on_deviation
        self.ledger.append("deviation", **self._ids(action),
                           data={"mismatches": mismatches, "score": score, "response": response})
        if response == "warn":
            return
        if response == "rollback":
            self.undo(action.id, by="auto-rollback", reason="outcome deviated from simulation")
        elif response == "halt":
            self.kill(session.id, reason=f"deviation in {action.id}", rollback=False, by="auto-rollback")
        elif response == "rollback_session":
            self.kill(session.id, reason=f"deviation in {action.id}", rollback=True, by="auto-rollback")
        raise DeviationDetected(action.id, mismatches, response)

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: (f"[redacted sha256:{digest(v)[:12]}]" if isinstance(k, str) and self.policy.redact_value(k)
                        else self._redact(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(v) for v in value]
        return value

    # ------------------------------------------------------------------ undo & rollback
    def _load(self, action_id: str):
        entries = self.ledger.for_action(action_id)
        if not entries:
            raise UnknownAction(action_id)
        head = entries[0]
        op = self.registry.get(f"{head['tool']}.{head['op']}")
        proposed = first(entries, "proposed")
        args = self.blobs.get_json(proposed["data"]["args_ref"]) if proposed else {}
        return entries, op, args

    def status(self, action_id: str) -> str:
        entries = self.ledger.for_action(action_id)
        if not entries:
            raise UnknownAction(action_id)
        return summarize(entries)["status"]

    def undo(self, action_id: str, *, force: bool = False, by: str = "undolith", reason: str = "") -> bool:
        """Apply an action's inverse. Returns False if it was already undone.

        Refuses (``UndoConflict``) when the observable state changed since commit,
        because undoing would clobber someone else's newer change.
        """
        entries, op, args = self._load(action_id)
        state = summarize(entries)["status"]
        if state == "undone":
            return False
        if state not in UNDOABLE:
            raise UndolithError(f"{action_id} is {state}; only committed, failed or in-flight actions can be undone")
        if op.undo is None or not op.is_reversible(args):
            raise NotReversible(f"{op.qualname} has no inverse")
        prepared, committed = last(entries, "prepared"), last(entries, "committed")
        snap_ref = prepared["data"].get("snapshot_ref") if prepared else None
        snapshot = self.blobs.get_json(snap_ref) if snap_ref else None
        result = self.blobs.get_json(committed["data"]["result_ref"]) if committed else None
        if committed is None and snapshot is None:
            raise NotReversible(f"{action_id} never committed and has no snapshot to restore")
        expected = committed["data"].get("observed") if committed else None
        if not force and expected is not None and op.observe is not None:
            current = self._observe(op, args, result)
            if _norm(current) != _norm(expected):
                raise UndoConflict(action_id, expected, current)
        ids = {k: entries[0][k] for k in ("action", "session", "agent", "tool", "op")}
        try:
            resolve(op.undo(args, snapshot, result))
        except Exception as exc:
            self.ledger.append("undo_failed", **ids, data={"error": f"{type(exc).__name__}: {exc}", "by": by})
            raise
        self.ledger.append("undone", **ids, data={"by": by, "reason": reason, "forced": force})
        return True

    def _session_actions(self, session_id: str) -> List[List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for e in self.ledger.entries(session=session_id):
            if e.get("action"):
                grouped.setdefault(e["action"], []).append(e)
        return list(grouped.values())

    def rollback(self, session_id: str, *, to: int = 0, force: bool = False, by: str = "undolith") -> RollbackReport:
        """Undo every undoable action in a session (after checkpoint ``to``), newest first."""
        report = RollbackReport(session=session_id)
        todo = []
        for entries in self._session_actions(session_id):
            prepared = first(entries, "prepared")
            if prepared and prepared["seq"] > to and summarize(entries)["status"] in UNDOABLE:
                todo.append((prepared["seq"], entries[0]["action"]))
        for _, action_id in sorted(todo, reverse=True):
            try:
                if self.undo(action_id, force=force, by=by, reason="session rollback"):
                    report.undone.append(action_id)
            except NotReversible as exc:
                report.skipped.append({"action": action_id, "reason": str(exc)})
            except Exception as exc:
                report.failed.append({"action": action_id, "error": f"{type(exc).__name__}: {exc}"})
        self.ledger.append("rollback", session=session_id, data={
            "to": to, "undone": report.undone, "skipped": report.skipped, "failed": report.failed, "by": by})
        return report

    # ------------------------------------------------------------------ kill switch
    @property
    def kill_file(self) -> Path:
        return self.home / "KILL"

    def kill(self, session_id: Optional[str] = None, *, reason: str = "", rollback: bool = True,
             by: str = "undolith") -> Optional[RollbackReport]:
        """Halt a session (and roll it back), or with no session, halt everything."""
        if session_id is None:
            self.kill_file.write_text(f"{utcnow()} {by}: {reason}\n")
            self.ledger.append("halted", data={"scope": "global", "reason": reason, "by": by})
            return None
        self.ledger.append("halted", session=session_id, data={"scope": "session", "reason": reason, "by": by})
        return self.rollback(session_id, by=by) if rollback else None

    def resume(self, session_id: Optional[str] = None, *, by: str = "undolith") -> None:
        if session_id is None:
            if self.kill_file.exists():
                self.kill_file.unlink()
            self.ledger.append("resumed", data={"scope": "global", "by": by})
        else:
            self.ledger.append("resumed", session=session_id, data={"scope": "session", "by": by})

    def is_halted(self, session_id: Optional[str] = None) -> bool:
        if self.kill_file.exists():
            return True
        if session_id is None:
            return False
        marks = self.ledger.entries(session=session_id, kind=("halted", "resumed"))
        return bool(marks) and marks[-1]["kind"] == "halted"

    def _ensure_running(self, session_id: str) -> None:
        if self.kill_file.exists():
            raise SessionHalted(f"global kill switch engaged ({self.kill_file})")
        if self.is_halted(session_id):
            raise SessionHalted(f"session {session_id} is halted")

    # ------------------------------------------------------------------ outbox (held actions)
    def held(self) -> List[Dict[str, Any]]:
        out = []
        for h in self.ledger.entries(kind="held"):
            entries = self.ledger.for_action(h["action"])
            if summarize(entries)["status"] != "held":
                continue
            sim = first(entries, "simulated")
            out.append({"action": h["action"], "qualname": f"{h['tool']}.{h['op']}", "session": h["session"],
                        "agent": h["agent"], "ts": h["ts"], "reason": h["data"].get("reason"),
                        "summary": sim["data"].get("summary") if sim else None})
        return out

    def release(self, action_id: str, *, by: str = "human", note: str = "") -> Any:
        """Approve a held action and commit it now."""
        entries, op, args = self._load(action_id)
        if summarize(entries)["status"] != "held":
            raise UndolithError(f"{action_id} is not held")
        head, proposed, sim = entries[0], first(entries, "proposed"), first(entries, "simulated")
        self._ensure_running(head["session"])
        session = Session(self, id=head["session"], agent=head["agent"])
        action = Action(action_id, head["session"], head["agent"], head["tool"], head["op"], args,
                        Risk.parse(proposed["data"]["risk"]))
        preview = None
        if sim:
            d = sim["data"]
            preview = Preview(summary=d.get("summary", ""), predicted=d.get("predicted") or {},
                              simulated=d.get("simulated", True), details=d.get("details") or {})
        self.ledger.append("released", **self._ids(action), data={"by": by, "note": note})
        return self._commit(session, op, action, preview)

    def discard(self, action_id: str, *, by: str = "human", reason: str = "") -> None:
        entries = self.ledger.for_action(action_id)
        if not entries:
            raise UnknownAction(action_id)
        if summarize(entries)["status"] != "held":
            raise UndolithError(f"{action_id} is not held")
        ids = {k: entries[0][k] for k in ("action", "session", "agent", "tool", "op")}
        self.ledger.append("discarded", **ids, data={"by": by, "reason": reason})

    # ------------------------------------------------------------------ audit
    def verify(self, action_id: str, *, segment: bool = False) -> Dict[str, Any]:
        """``verify(action_id) -> proof``: did it happen, was it authorized, signed evidence."""
        from .proof import build_proof

        return build_proof(self.ledger, action_id, segment=segment)

    def verify_chain(self, *, blobs: bool = True) -> ChainReport:
        return self.ledger.verify_chain(blobs=self.blobs if blobs else None)

    def actions(self, session: Optional[str] = None) -> List[Dict[str, Any]]:
        """One summary row per action, oldest first."""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for e in self.ledger.entries(session=session):
            if e.get("action"):
                grouped.setdefault(e["action"], []).append(e)
        rows = []
        for action_id, entries in grouped.items():
            h, s = entries[0], summarize(entries)
            proposed = first(entries, "proposed") or first(entries, "read")
            rows.append({"action": action_id, "ts": h["ts"], "session": h["session"], "agent": h["agent"],
                         "qualname": f"{h['tool']}.{h['op']}", "status": s["status"],
                         "risk": (proposed or {}).get("data", {}).get("risk", "read"),
                         "authorized": s["authorized"], "deviated": s["deviated"]})
        return rows

    def replay(self, session_id: str, into: "Undolith") -> List[ReplayStep]:
        """Re-run a session's committed actions inside another (sandboxed) Undolith.

        Results are compared by content hash, and actions that were undone in the
        original are undone in the replay too, so the sandbox ends in the same state.
        """
        steps = []
        replay_session = into.session(f"replay-{session_id}", agent="replay")
        for entries in self._session_actions(session_id):
            committed = first(entries, "committed")
            if committed is None:
                continue
            h = entries[0]
            qual = f"{h['tool']}.{h['op']}"
            args = self.blobs.get_json(first(entries, "proposed")["data"]["args_ref"])
            step = ReplayStep(entries[0]["action"], qual, committed["data"].get("result_sha256"),
                              undone=summarize(entries)["status"] == "undone")
            try:
                before = into.ledger.head()
                result = replay_session.call(qual, **args)
                step.replay_sha256 = digest(result)
                if step.undone:
                    new = [e for e in into.ledger.entries(since=before["seq"] if before else 0, kind="committed")]
                    if new:
                        into.undo(new[-1]["action"], by="replay")
            except Exception as exc:
                step.error = f"{type(exc).__name__}: {exc}"
            steps.append(step)
        return steps

    @property
    def signer(self) -> Signer:
        return self.ledger.signer


def _as_approval(value: Any) -> Approval:
    if isinstance(value, Approval):
        return value
    return Approval(approved=bool(value))


def _norm(value: Any) -> str:
    return digest(value)
