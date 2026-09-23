"""Ready-made approvers. An approver is any ``callable(action, preview) -> bool | Approval``."""

from __future__ import annotations

import sys
from typing import Callable, Optional

from ._canon import short
from .model import Action, Approval, Preview


class ConsoleApprover:
    """Ask a human on the terminal, showing the simulated diff first."""

    def __init__(self, name: str = "console", stream=None, input_fn: Callable[[str], str] = input):
        self.name = name
        self.stream = stream or sys.stderr
        self.input_fn = input_fn

    def __call__(self, action: Action, preview: Optional[Preview]) -> Approval:
        w = self.stream.write
        w(f"\n── undolith: approval needed ─ {action.qualname} ({action.risk.label}) ─ {action.id}\n")
        w(f"   agent={action.agent} session={action.session}\n")
        w(f"   args: {short(action.args, 300)}\n")
        if preview is not None:
            w(f"   preview: {preview.summary}\n")
            if preview.diff:
                for line in preview.diff.splitlines()[:60]:
                    w(f"   │ {line}\n")
        answer = self.input_fn("   approve? [y/N] ").strip().lower()
        return Approval(answer in ("y", "yes"), by=self.name)


class AutoApprove:
    def __init__(self, name: str = "auto"):
        self.name = name

    def __call__(self, action: Action, preview: Optional[Preview]) -> Approval:
        return Approval(True, by=self.name, note="auto-approved")


class AutoReject:
    def __init__(self, name: str = "auto", note: str = "auto-rejected"):
        self.name, self.note = name, note

    def __call__(self, action: Action, preview: Optional[Preview]) -> Approval:
        return Approval(False, by=self.name, note=self.note)
