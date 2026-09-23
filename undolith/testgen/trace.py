"""The normalised trace every importer produces and every judge reads."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .._canon import canonical

# Step statuses. Anything other than "ok" and "error" came from an Undolith ledger.
OK, ERROR = "ok", "error"
BLOCKED = ("denied", "rejected", "discarded")  # the guard stopped it
REVERSED = ("undone", "deviation")  # it happened and had to be taken back
BAD = BLOCKED + REVERSED + ("failed",)
ROLLED_BACK = "rolled_back"  # undone only because its whole session was rolled back (collateral, not itself bad)
REPLAYABLE = (OK, ERROR, ROLLED_BACK)  # steps whose recorded results can be served to an agent under test


@dataclass
class Step:
    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: Optional[str] = None
    status: str = OK
    risk: Optional[str] = None
    thought: str = ""
    ref: Optional[str] = None  # action id, tool-call id or turn number in the source

    @property
    def key(self) -> str:
        return f"{self.name}:{canonical(self.args)}"


@dataclass
class Trace:
    id: str
    task: str = ""
    steps: List[Step] = field(default_factory=list)
    final: Optional[str] = None
    source: str = ""
    outcome: Optional[str] = None  # "success" | "failure" | None (unknown: let a judge decide)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Trace":
        steps = [s if isinstance(s, Step) else Step(**s) for s in d.get("steps", [])]
        return cls(**{**{k: v for k, v in d.items() if k != "steps"}, "steps": steps})

    def render(self, max_chars: int = 6000, result_chars: int = 300) -> str:
        """Compact, numbered text form for LLM judges."""
        lines = [f"TASK: {self.task.strip()[:1500]}"]
        for i, s in enumerate(self.steps, 1):
            args = canonical(s.args)
            if len(args) > 400:
                args = args[:399] + "…"
            if s.error:
                out = f"ERROR: {s.error}"
            else:
                out = s.result if isinstance(s.result, str) else canonical(s.result)
            out = (out or "").replace("\n", " ")
            if len(out) > result_chars:
                out = out[: result_chars - 1] + "…"
            tag = "" if s.status == OK else f" [{s.status.upper()}]"
            lines.append(f"STEP {i}: {s.name} {args}{tag} -> {out}")
        lines.append(f"FINAL ANSWER: {self.final if self.final is not None else '(none)'}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            head = max_chars // 3
            text = text[:head] + "\n…(middle steps omitted)…\n" + text[-(max_chars - head):]
        return text
