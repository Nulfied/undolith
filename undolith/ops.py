"""Operations, adapters and the registry.

An :class:`Operation` is the unit Undolith guards. It bundles the real call with
the optional hooks that make it simulate-able, snapshot-able and undo-able::

    run(**args)                       -> result          # the real side effect
    simulate(**args)                  -> Preview         # must not have side effects
    snapshot(**args)                  -> JSON            # pre-state needed to undo
    undo(args, snapshot, result)      -> None            # the inverse
    observe(args, result)             -> dict            # observable post-state
    reversible(args)                  -> bool            # per-call reversibility (defaults to: undo exists)

Any hook may be a coroutine function; Undolith resolves awaitables for you.
"""

from __future__ import annotations

import asyncio
import inspect
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional

from .model import Preview, Risk, UnknownOperation

_loop: ContextVar[Optional[asyncio.AbstractEventLoop]] = ContextVar("undolith_loop", default=None)


async def _await(value):
    return await value


def resolve(value: Any) -> Any:
    """Turn an awaitable into its result, from sync code, even inside a worker thread."""
    if not inspect.isawaitable(value):
        return value
    loop = _loop.get()
    if loop is not None and loop.is_running():
        return asyncio.run_coroutine_threadsafe(_await(value), loop).result()
    return asyncio.run(_await(value))


@dataclass
class Operation:
    tool: str
    name: str
    run: Callable[..., Any]
    risk: Any = Risk.WRITE  # Risk, "write", or callable(args) -> Risk
    simulate: Optional[Callable[..., Preview]] = None
    snapshot: Optional[Callable[..., Any]] = None
    undo: Optional[Callable[[Dict[str, Any], Any, Any], Any]] = None
    observe: Optional[Callable[[Dict[str, Any], Any], Dict[str, Any]]] = None
    reversible: Optional[Callable[[Dict[str, Any]], bool]] = None
    description: str = ""

    @property
    def qualname(self) -> str:
        return f"{self.tool}.{self.name}"

    def risk_for(self, args: Dict[str, Any]) -> Risk:
        risk = self.risk(args) if callable(self.risk) else self.risk
        return Risk.parse(risk)

    def is_reversible(self, args: Dict[str, Any]) -> bool:
        if self.undo is None:
            return False
        return bool(self.reversible(args)) if self.reversible is not None else True


class Adapter:
    """Base class for a tool made of several operations."""

    tool: str = "tool"

    def operations(self) -> List[Operation]:  # pragma: no cover - interface
        raise NotImplementedError

    def op(self, name: str, run: Callable[..., Any], **hooks) -> Operation:
        return Operation(tool=self.tool, name=name, run=run, **hooks)


class Registry:
    def __init__(self):
        self._ops: Dict[str, Operation] = {}

    def add(self, *items: Any) -> "Registry":
        for item in items:
            if isinstance(item, Operation):
                self._ops[item.qualname] = item
            elif isinstance(item, Adapter):
                self.add(*item.operations())
            elif isinstance(item, (list, tuple, set)):
                self.add(*item)
            else:
                raise TypeError(f"cannot register {item!r}; expected Operation or Adapter")
        return self

    def get(self, qualname: str) -> Operation:
        try:
            return self._ops[qualname]
        except KeyError:
            raise UnknownOperation(f"no operation registered as {qualname!r}") from None

    def __contains__(self, qualname: str) -> bool:
        return qualname in self._ops

    def __iter__(self) -> Iterator[Operation]:
        return iter(self._ops.values())

    def names(self) -> List[str]:
        return sorted(self._ops)
