"""MCP adapter: guard every ``tools/call`` an agent makes through an MCP ClientSession.

    from undolith.integrations.mcp import GuardedSession
    async with ClientSession(read, write) as raw:
        await raw.initialize()
        session = GuardedSession(guard, raw, server="github")
        await session.list_tools()              # learns risk from MCP tool annotations
        await session.call_tool("create_issue", {...})   # simulated, logged, signed

Risk comes from MCP tool annotations when the server provides them
(``readOnlyHint``, ``destructiveHint``, ``openWorldHint``), falling back to the
name heuristic. Annotations are hints from the server, so an explicit ``risks``
override always wins.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from ..classify import classify_name
from ..core import Undolith
from ..model import Risk
from ..ops import Operation


def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def risk_from_annotations(annotations: Any, name: str = "", description: str = "") -> Risk:
    heuristic = classify_name(name, description or "")
    if annotations is None:
        return heuristic
    if _get(annotations, "readOnlyHint") is True:
        return Risk.READ
    destructive = _get(annotations, "destructiveHint")
    open_world = _get(annotations, "openWorldHint")
    if destructive is False:
        risk = Risk.WRITE
    else:  # MCP spec: destructiveHint defaults to true for non-read-only tools
        risk = Risk.DESTRUCTIVE
    if open_world is True and heuristic is Risk.IRREVERSIBLE:
        risk = Risk.IRREVERSIBLE
    return max(risk, heuristic) if heuristic is not Risk.READ else risk


class GuardedSession:
    """Wraps an MCP ``ClientSession``; everything except ``call_tool`` passes through."""

    def __init__(self, guard: Undolith, session: Any, *, server: str = "mcp",
                 risks: Optional[Dict[str, Any]] = None, inverses: Optional[Dict[str, Callable]] = None,
                 simulators: Optional[Dict[str, Callable]] = None, snapshots: Optional[Dict[str, Callable]] = None):
        self._guard, self._session, self._server = guard, session, server
        self._risks, self._inverses = risks or {}, inverses or {}
        self._simulators, self._snapshots = simulators or {}, snapshots or {}

    def _register(self, name: str, annotations: Any = None, description: str = "") -> Operation:
        qual = f"{self._server}.{name}"
        if qual in self._guard.registry:
            return self._guard.registry.get(qual)
        session = self._session

        def run(**arguments):
            return session.call_tool(name, arguments)  # a coroutine; Undolith resolves it on the caller's loop

        risk = self._risks.get(name)
        op = Operation(tool=self._server, name=name, run=run,
                       risk=risk if risk is not None else risk_from_annotations(annotations, name, description),
                       simulate=self._simulators.get(name), snapshot=self._snapshots.get(name),
                       undo=self._inverses.get(name), description=description)
        self._guard.register(op)
        return op

    async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._session.list_tools(*args, **kwargs)
        for tool in _get(result, "tools") or []:
            self._register(_get(tool, "name"), _get(tool, "annotations"), _get(tool, "description") or "")
        return result

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None, *args: Any, **kwargs: Any) -> Any:
        op = self._register(name)
        return await self._guard.acall(op.qualname, **(arguments or {}))

    def __getattr__(self, item: str) -> Any:
        return getattr(self._session, item)
