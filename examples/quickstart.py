"""Thirty-second tour: guard a function, watch it get simulated, logged, proven and undone.

    python examples/quickstart.py
"""

import tempfile
from pathlib import Path

from undolith import Preview, Undolith, verify_proof

home = Path(tempfile.mkdtemp()) / ".undolith"
guard = Undolith(home)
todos = {}


@guard.tool("todo.add")  # risk inferred from the verb: "add" -> write
def add_todo(title: str) -> dict:
    todo_id = f"t{len(todos) + 1}"
    todos[todo_id] = title
    return {"id": todo_id}


@add_todo.simulator
def _(title):
    return Preview(f"would add todo {title!r}", predicted={"count": len(todos) + 1})


@add_todo.observer
def _(args, result):
    return {"count": len(todos)}


@add_todo.inverse
def _(args, snapshot, result):
    del todos[result["id"]]


with guard.session(agent="demo") as session:
    add_todo("write the spec")
    add_todo("ship it")
    print("todos:", todos)

    first = guard.actions()[0]["action"]
    proof = guard.verify(first, segment=True)
    print("proof valid:", verify_proof(proof, public_key=guard.signer.public_key).valid)

    session.rollback()
    print("after rollback:", todos)

print("chain intact:", guard.verify_chain().ok, f"({guard.verify_chain().length} entries in {home})")
