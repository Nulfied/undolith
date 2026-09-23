"""Turn agent logs from anywhere into :class:`Trace` objects.

Supported inputs:

* an **Undolith ledger** (``from_ledger``): statuses such as denied, undone or
  deviation come for free, so failures are already labelled;
* **OpenAI-style** chat messages with ``tool_calls`` / ``role: tool``;
* **Anthropic-style** messages with ``tool_use`` / ``tool_result`` blocks;
* **ShareGPT / ReAct** conversations, e.g. THUDM's AgentInstruct (AgentBench
  OS, DB, KG, ALFWorld, WebShop, Mind2Web): ``Act: bash`` + code fence,
  ``Action: search[...]``, ``ACTION: go to ...``, ``Act: answer(...)``;
* the native trace JSON written by :meth:`Trace.to_dict`.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from .trace import ERROR, OK, ROLLED_BACK, Step, Trace

FINAL_ACTIONS = {"answer", "finish", "final answer", "final_answer", "commit", "submit"}


# ----------------------------------------------------------------------------- Undolith ledger
def from_ledger(source: Any, session: Optional[str] = None, *, include_reads: bool = True) -> List[Trace]:
    """One trace per session. Arguments come from the *redacted* ledger view, never the raw blobs,
    so secrets do not leak into generated test suites."""
    from ..core import Undolith
    from ..lifecycle import first, last, summarize

    guard = source if isinstance(source, Undolith) else Undolith(source)
    entries = guard.ledger.entries(session=session) if session else guard.ledger.entries()
    sessions: Dict[str, List[Dict[str, Any]]] = {}
    for e in entries:
        if e.get("session"):
            sessions.setdefault(e["session"], []).append(e)

    traces = []
    for sid, sentries in sessions.items():
        started, finished = first(sentries, "started"), last(sentries, "finished")
        by_action: Dict[str, List[Dict[str, Any]]] = {}
        for e in sentries:
            if e.get("action"):
                by_action.setdefault(e["action"], []).append(e)
        steps, bad = [], False
        for aid, aentries in by_action.items():
            h = aentries[0]
            state = summarize(aentries)
            read, proposed = first(aentries, "read"), first(aentries, "proposed")
            if read is not None and not include_reads:
                continue
            carrier = read or proposed or h
            data = carrier.get("data") or {}
            status = {"committed": OK, "undone": "undone", "denied": "denied", "rejected": "rejected",
                      "discarded": "discarded", "failed": "failed", "held": "held"}.get(state["status"], state["status"])
            if state["deviated"] and status in (OK, "undone"):
                status = "deviation"
            elif status == "undone" and (last(aentries, "undone") or {}).get("data", {}).get("reason") == "session rollback":
                status = ROLLED_BACK
            result, error = None, None
            committed = first(aentries, "committed")
            ref = (read or {}).get("data", {}).get("result_ref") or (committed or {}).get("data", {}).get("result_ref")
            if ref and guard.blobs.exists(ref):
                result = guard._redact(guard.blobs.get_json(ref))
            failed = first(aentries, "failed")
            if failed:
                error = failed["data"].get("error")
            for kind in ("denied", "rejected", "deviation"):
                hit = first(aentries, kind)
                if hit:
                    error = error or hit["data"].get("reason") or hit["data"].get("note") or json.dumps(
                        hit["data"].get("mismatches"))
            bad = bad or status not in (OK, "held", ROLLED_BACK)
            steps.append(Step(name=f"{h['tool']}.{h['op']}", args=data.get("args") or {}, result=result, error=error,
                              status=status, risk=data.get("risk", "read" if read else None), ref=aid))
        halted = any(e["kind"] == "halted" for e in sentries)
        claimed = (finished or {}).get("data", {}).get("outcome")
        outcome = claimed or ("failure" if bad or halted else ("success" if finished else None))
        answer = (finished or {}).get("data", {}).get("answer")
        traces.append(Trace(
            id=sid, task=(started or {}).get("data", {}).get("task", ""), steps=steps,
            final=None if answer is None else str(answer), source="undolith-ledger", outcome=outcome,
            meta={"agent": sentries[0].get("agent"), "halted": halted,
                  "rolled_back": any(e["kind"] == "rollback" for e in sentries)},
        ))
    return traces


# ----------------------------------------------------------------------------- OpenAI / Anthropic
def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content
                         if not isinstance(b, dict) or b.get("type") in (None, "text", "input_text", "output_text"))
    return str(content)


def _looks_like_error(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith(("error", "traceback", "exception")) or '"error"' in t[:200]


def from_openai_messages(messages: List[Dict[str, Any]], *, id: str = "trace", source: str = "openai") -> Trace:
    task, steps, final = "", [], None
    pending: Dict[str, Step] = {}
    for m in messages:
        role = m.get("role")
        if role == "user" and not task:
            task = _text(m.get("content"))
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            for c in calls:
                fn = c.get("function", c)
                raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except json.JSONDecodeError:
                    args = {"input": raw}
                step = Step(name=fn.get("name", "?"), args=args if isinstance(args, dict) else {"input": args},
                            thought=_text(m.get("content")), ref=c.get("id"))
                steps.append(step)
                pending[c.get("id") or str(len(steps))] = step
            if not calls and m.get("content"):
                final = _text(m.get("content"))
        elif role == "tool":
            step = pending.get(m.get("tool_call_id")) or (steps[-1] if steps else None)
            if step is not None:
                out = _text(m.get("content"))
                step.result = out
                if _looks_like_error(out):
                    step.status, step.error = ERROR, out[:500]
    return Trace(id=id, task=task, steps=steps, final=final, source=source)


def from_anthropic_messages(messages: List[Dict[str, Any]], *, id: str = "trace", source: str = "anthropic") -> Trace:
    task, steps, final = "", [], None
    by_id: Dict[str, Step] = {}
    for m in messages:
        content = m.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        if m.get("role") == "user":
            for b in blocks:
                if b.get("type") == "tool_result":
                    step = by_id.get(b.get("tool_use_id"))
                    if step is not None:
                        out = _text(b.get("content"))
                        step.result = out
                        if b.get("is_error") or _looks_like_error(out):
                            step.status, step.error = ERROR, out[:500]
                elif b.get("type") == "text" and not task:
                    task = b.get("text", "")
        elif m.get("role") == "assistant":
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            uses = [b for b in blocks if b.get("type") == "tool_use"]
            for u in uses:
                step = Step(name=u.get("name", "?"), args=dict(u.get("input") or {}), thought="\n".join(texts),
                            ref=u.get("id"))
                steps.append(step)
                by_id[u.get("id")] = step
            if not uses and texts:
                final = "\n".join(texts)
    return Trace(id=id, task=task, steps=steps, final=final, source=source)


# ----------------------------------------------------------------------------- ShareGPT / ReAct
_ACTION = re.compile(r"(?:^|\n)\s*(?:act|action)\s*:\s*", re.IGNORECASE)
_FENCE = re.compile(r"```[\w+-]*\n(.*?)```", re.DOTALL)
_CALL = re.compile(r"^([\w .-]+?)\s*[\[(](.*)[\])]\s*$", re.DOTALL)
_THOUGHT = re.compile(r"^\s*(?:think|thought)\s*:\s*", re.IGNORECASE)


def parse_react_action(text: str) -> Optional[Tuple[str, Dict[str, Any], bool, str]]:
    """Parse one model turn. Returns ``(name, args, is_final, thought)`` or None when there is no action."""
    m = None
    for m in _ACTION.finditer(text):
        pass  # the last "Action:" in the turn wins
    if m is None:
        return None
    thought = _THOUGHT.sub("", text[: m.start()]).strip()
    body = text[m.end():].strip()
    fence = _FENCE.search(body)
    head = body[: fence.start()].strip() if fence else body.split("\n", 1)[0].strip()
    if fence:
        name, args = (head.split()[0].lower() if head else "code"), {"input": fence.group(1).strip()}
    elif head.lower().startswith("final answer"):
        name, args = "final answer", {"input": head.split(":", 1)[-1].strip()}
    else:
        call = _CALL.match(head)
        if call:
            name, args = call.group(1).strip().lower(), {"input": call.group(2).strip()}
        elif " " in head:
            verb, rest = head.split(" ", 1)
            name, args = verb.lower(), {"input": rest.strip()}
        else:
            name, args = head.lower(), {}
    if name == "answer" and not args and "final answer" in body.lower():
        args = {"input": body.lower().split("final answer", 1)[1].lstrip(": ").strip()}
    return name, args, name in FINAL_ACTIONS, thought


def from_sharegpt(record: Dict[str, Any], *, source: str = "sharegpt") -> Trace:
    """ShareGPT conversation (``from``/``value``). Turns marked ``loss: false`` are few-shot demos and skipped."""
    turns = record.get("conversations") or record.get("messages") or []
    role = lambda t: (t.get("from") or t.get("role") or "").lower()  # noqa: E731
    text = lambda t: t.get("value") if "value" in t else _text(t.get("content"))  # noqa: E731
    model_turns = [i for i, t in enumerate(turns) if role(t) in ("gpt", "assistant", "model")]
    has_loss = any(turns[i].get("loss") is True for i in model_turns)
    real = [i for i in model_turns if turns[i].get("loss") is True] if has_loss else model_turns
    start = real[0] if real else len(turns)
    task = next((text(turns[i]) for i in range(start - 1, -1, -1) if role(turns[i]) in ("human", "user")), "")
    steps, final = [], None
    for i in real:
        parsed = parse_react_action(text(turns[i]) or "")
        if parsed is None:
            final = final or (text(turns[i]) or "").strip() or None
            continue
        name, args, is_final, thought = parsed
        if is_final:
            final = args.get("input", "") if args else ""
            break
        obs = text(turns[i + 1]) if i + 1 < len(turns) and role(turns[i + 1]) in ("human", "user") else None
        step = Step(name=name, args=args, result=obs, thought=thought, ref=f"turn {i}")
        if obs and _looks_like_error(obs):
            step.status, step.error = ERROR, obs[:500]
        steps.append(step)
        if obs is None and i == real[-1]:  # the episode ended on this action (e.g. WebShop's click[Buy Now])
            final = f"{name}[{args['input']}]" if "input" in args else name
    return Trace(id=str(record.get("id", "trace")), task=task, steps=steps, final=final, source=source,
                 meta={k: v for k, v in record.items() if k not in ("conversations", "messages")})


# ----------------------------------------------------------------------------- OSWorld
OSWORLD_SPECIAL = {"DONE", "FAIL", "WAIT"}


def from_osworld(example_dir: Union[str, Path], *, examples_root: Union[str, Path, None] = None) -> Trace:
    """One OSWorld result directory (``traj.jsonl`` + ``result.txt``) → trace.

    Layout written by OSWorld's runner: ``results/<action_space>/<obs>/<model>/<domain>/<example_id>/``.
    The task text lives in ``evaluation_examples/examples/<domain>/<example_id>.json``; pass
    ``examples_root`` (that ``examples`` folder) to fill it in. Screenshots are not imported.
    """
    d = Path(example_dir)
    task, steps, final = "", [], None
    for line in (d / "traj.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if "instruction" in rec and "step_num" not in rec:
            task = task or rec["instruction"]
            continue
        action = rec.get("action")
        text = action if isinstance(action, str) else json.dumps(action, sort_keys=True)
        if isinstance(action, str) and action.strip().upper() in OSWORLD_SPECIAL:
            word = action.strip().upper()
            if word in ("DONE", "FAIL"):
                final = word
            continue
        info = rec.get("info") or {}
        error = info.get("error") if isinstance(info, dict) else None
        steps.append(Step(name="pyautogui" if isinstance(action, str) else str(action.get("action_type", "action")),
                          args={"input": text}, result={"reward": rec.get("reward"), "done": rec.get("done")},
                          error=error, status=ERROR if error else OK,
                          thought=str(rec.get("response") or "")[:2000], ref=f"step {rec.get('step_num')}"))
    if not task and examples_root is not None:
        spec = Path(examples_root) / d.parent.name / f"{d.name}.json"
        if spec.exists():
            task = json.loads(spec.read_text(encoding="utf-8")).get("instruction", "")
    score = None
    if (d / "result.txt").exists():
        try:
            score = float((d / "result.txt").read_text().strip())
        except ValueError:
            pass
    outcome = None if score is None else ("success" if score >= 1.0 else "failure")
    return Trace(id=f"{d.parent.name}/{d.name}", task=task, steps=steps, final=final, source="osworld",
                 outcome=outcome, meta={"score": score, "domain": d.parent.name})


def load_osworld(results_root: Union[str, Path], *, examples_root: Union[str, Path, None] = None,
                 limit: Optional[int] = None) -> List[Trace]:
    dirs = sorted(p.parent for p in Path(results_root).rglob("traj.jsonl"))
    return [from_osworld(p, examples_root=examples_root) for p in dirs[:limit]]


# ----------------------------------------------------------------------------- files & datasets
def detect_format(record: Dict[str, Any]) -> str:
    if "steps" in record and "task" in record:
        return "trace"
    if "conversations" in record:
        return "sharegpt"
    msgs = record.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            c = m.get("content")
            if m.get("tool_calls") or m.get("role") == "tool":
                return "openai"
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result")
                                           for b in c):
                return "anthropic"
            if "from" in m:
                return "sharegpt"
        return "openai"
    raise ValueError(f"cannot detect trace format from keys {sorted(record)}")


def from_record(record: Dict[str, Any], fmt: str = "auto", *, id: Optional[str] = None) -> Trace:
    fmt = detect_format(record) if fmt == "auto" else fmt
    rid = id or str(record.get("id", "trace"))
    if fmt == "trace":
        return Trace.from_dict(record)
    if fmt == "sharegpt":
        return from_sharegpt(record)
    if fmt == "openai":
        return from_openai_messages(record["messages"], id=rid)
    if fmt == "anthropic":
        return from_anthropic_messages(record["messages"], id=rid)
    raise ValueError(f"unknown format {fmt!r}")


def _records(path: Path) -> Iterable[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        for line in text.splitlines():
            if line.strip():
                yield json.loads(line)
        return
    data = json.loads(text)
    if isinstance(data, list):
        yield from data
    elif isinstance(data, dict) and isinstance(data.get("rows"), list):  # HF datasets-server response
        for r in data["rows"]:
            yield r.get("row", r)
    else:
        yield data


def load_traces(path: Union[str, Path], fmt: str = "auto", *, limit: Optional[int] = None) -> List[Trace]:
    """A json/jsonl file of traces, an OSWorld ``traj.jsonl``, or a directory of OSWorld results."""
    p = Path(path)
    if fmt == "osworld" or p.is_dir() or p.name == "traj.jsonl":
        if p.is_dir():
            return load_osworld(p, limit=limit)
        return [from_osworld(p.parent)]
    out = []
    for n, rec in enumerate(_records(Path(path))):
        if limit is not None and n >= limit:
            break
        out.append(from_record(rec, fmt, id=str(rec.get("id", f"{Path(path).stem}-{n}"))))
    return out


def fetch_hf_rows(dataset: str, split: str, *, offset: int = 0, length: int = 20,
                  config: str = "default", timeout: float = 60) -> List[Dict[str, Any]]:
    """Read rows of a public Hugging Face dataset through the free datasets-server API (no key, no download of files)."""
    rows: List[Dict[str, Any]] = []
    while len(rows) < length:
        n = min(100, length - len(rows))
        query = urllib.parse.urlencode({"dataset": dataset, "config": config, "split": split,
                                        "offset": offset + len(rows), "length": n})
        with urllib.request.urlopen(f"https://datasets-server.huggingface.co/rows?{query}", timeout=timeout) as r:
            page = json.loads(r.read().decode("utf-8"))
        if "rows" not in page:
            raise RuntimeError(page.get("error", "unexpected response from datasets-server"))
        batch = [r["row"] for r in page["rows"]]
        rows.extend(batch)
        if len(batch) < n:
            break
    return rows
