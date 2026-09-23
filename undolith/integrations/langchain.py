"""LangChain adapter (duck-typed: importing this module does not import LangChain).

    from undolith.integrations.langchain import guard_tools
    tools = guard_tools(guard, [search_tool, write_file_tool], undo={"write_file": restore})
    agent = create_react_agent(llm, tools)

Each tool is wrapped *in place*: ``tool.func`` (``Tool``/``StructuredTool``) or
``tool._run`` (custom ``BaseTool`` subclasses) is replaced with a guarded call.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional

from ..classify import classify_name
from ..core import Undolith
from ..ops import Operation


def guard_tool(guard: Undolith, tool: Any, *, risk: Any = None, simulate: Optional[Callable] = None,
               snapshot: Optional[Callable] = None, undo: Optional[Callable] = None,
               observe: Optional[Callable] = None, namespace: str = "lc") -> Any:
    name = tool.name
    description = getattr(tool, "description", "") or ""
    func = getattr(tool, "func", None)
    uses_func = callable(func)
    original = func if uses_func else tool._run

    def run(**kwargs):
        if list(kwargs) == ["__input"]:  # single-input tool called with one positional string
            return original(kwargs["__input"])
        return original(**kwargs)

    op = Operation(tool=namespace, name=name, run=run,
                   risk=risk if risk is not None else classify_name(name, description),
                   simulate=simulate, snapshot=snapshot, undo=undo, observe=observe, description=description)
    guard.register(op)

    def guarded(*args, **kwargs):
        kwargs.pop("run_manager", None)
        kwargs.pop("callbacks", None)
        kwargs.pop("config", None)
        if len(args) == 1 and not kwargs:
            kwargs = {"__input": args[0]}  # single-string Tool
        elif args:
            raise TypeError(f"{name}: positional arguments are only supported for single-input tools")
        return guard.call(op.qualname, **kwargs)

    object.__setattr__(tool, "func" if uses_func else "_run", guarded)
    object.__setattr__(tool, "_undolith_operation", op)
    return tool


def guard_tools(guard: Undolith, tools: Iterable[Any], *, risk: Optional[Dict[str, Any]] = None,
                undo: Optional[Dict[str, Callable]] = None, simulate: Optional[Dict[str, Callable]] = None,
                snapshot: Optional[Dict[str, Callable]] = None) -> List[Any]:
    risk, undo, simulate, snapshot = risk or {}, undo or {}, simulate or {}, snapshot or {}
    return [guard_tool(guard, t, risk=risk.get(t.name), undo=undo.get(t.name), simulate=simulate.get(t.name),
                       snapshot=snapshot.get(t.name)) for t in tools]
