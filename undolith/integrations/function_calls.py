"""Dispatch model-emitted tool calls (any provider) through Undolith.

    for block in response.content:
        if block.type == "tool_use":
            output = dispatch(guard, block.name, block.input)

Undolith errors are turned into plain-text tool results so the model learns
that an action was denied, held or rolled back instead of crashing the loop.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Union

from ..core import Session, Undolith
from ..model import ActionDenied, ActionRejected, DeviationDetected, Held, SessionHalted


def dispatch(guard: Undolith, name: str, arguments: Union[str, Dict[str, Any], None], *,
             session: Optional[Session] = None, alias: Optional[Dict[str, str]] = None,
             as_text: bool = True) -> Any:
    """Run one tool call. ``alias`` maps model-facing names to registered ``tool.op`` names."""
    args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
    qual = (alias or {}).get(name, name)
    target = session or guard.current
    try:
        result = target.call(qual, **args)
    except ActionDenied as exc:
        return f"[undolith] DENIED by policy: {exc.reason}. Do not retry this action."
    except ActionRejected as exc:
        return f"[undolith] REJECTED by the human approver: {exc.reason}."
    except DeviationDetected as exc:
        return f"[undolith] The action did not do what its simulation predicted and was {exc.response}: {exc.mismatches}"
    except SessionHalted as exc:
        return f"[undolith] HALTED: {exc}. Stop and report to the user."
    if isinstance(result, Held):
        return (f"[undolith] HELD for human review as {result.action_id} ({result.reason}). "
                "It has NOT happened yet; continue without assuming it did.")
    if not as_text or isinstance(result, str):
        return result
    return json.dumps(result, default=str)
