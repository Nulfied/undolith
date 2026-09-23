import asyncio
from types import SimpleNamespace

import pytest

from undolith import ActionDenied, Held, Preview, Risk, Rule, Undolith, classify_name
from undolith.integrations.function_calls import dispatch
from undolith.integrations.langchain import guard_tool
from undolith.integrations.mcp import GuardedSession, risk_from_annotations


def test_classify_name():
    assert classify_name("get_user") is Risk.READ
    assert classify_name("listFiles") is Risk.READ
    assert classify_name("create_ticket") is Risk.WRITE
    assert classify_name("delete_repo") is Risk.DESTRUCTIVE
    assert classify_name("send_email") is Risk.IRREVERSIBLE
    assert classify_name("email_send") is Risk.IRREVERSIBLE
    assert classify_name("get_order") is Risk.READ
    assert classify_name("update_record", "Permanently deletes old rows") is Risk.DESTRUCTIVE


def test_decorator_with_inverse(tmp_path):
    guard = Undolith(tmp_path / "u")
    tickets = {}

    @guard.tool("tickets.create")
    def create_ticket(title: str, priority: str = "low"):
        tid = f"T{len(tickets) + 1}"
        tickets[tid] = {"title": title, "priority": priority}
        return {"id": tid}

    @create_ticket.inverse
    def _(args, snapshot, result):
        tickets.pop(result["id"])

    @create_ticket.simulator
    def _(title, priority="low"):
        return Preview(f"create ticket {title!r} ({priority})")

    assert create_ticket("printer on fire") == {"id": "T1"}
    assert create_ticket.operation.risk is Risk.WRITE
    entries = guard.ledger.for_action(guard.actions()[-1]["action"])
    assert entries[0]["data"]["args"] == {"title": "printer on fire", "priority": "low"}
    assert "printer on fire" in entries[1]["data"]["summary"]
    guard.undo(entries[0]["action"])
    assert tickets == {}


def test_async_tool_and_async_undo(tmp_path):
    guard = Undolith(tmp_path / "u")
    store = []

    @guard.tool("kv.put", risk="write")
    async def put(key, value):
        await asyncio.sleep(0)
        store.append((key, value))
        return len(store)

    @put.inverse
    async def _(args, snap, result):
        await asyncio.sleep(0)
        store.remove((args["key"], args["value"]))

    async def main():
        assert await put("a", 1) == 1
        return guard.actions()[-1]["action"]

    aid = asyncio.run(main())
    assert store == [("a", 1)]
    guard.undo(aid)  # sync undo of an async inverse
    assert store == []


class FakeStructuredTool:
    def __init__(self, name, func, description=""):
        self.name, self.func, self.description = name, func, description

    def run(self, tool_input):
        if isinstance(tool_input, dict):
            return self.func(**tool_input, callbacks=None)
        return self.func(tool_input)


class FakeBaseTool:
    name = "search_docs"
    description = "Search internal docs"

    def _run(self, query, run_manager=None):
        return f"results for {query}"

    def run(self, tool_input):
        return self._run(tool_input, run_manager=object())


def test_langchain_tools_are_guarded(tmp_path):
    guard = Undolith(tmp_path / "u")
    files = {}
    write = FakeStructuredTool("write_file", lambda path, text: files.update({path: text}) or "ok",
                               "Write text to a file")
    guard_tool(guard, write, undo=lambda args, snap, result: files.pop(args["path"]))
    assert write.run({"path": "a.md", "text": "hi"}) == "ok"
    assert files == {"a.md": "hi"}
    search = guard_tool(guard, FakeBaseTool())
    assert search.run("undo") == "results for undo"
    rows = guard.actions()
    assert [r["qualname"] for r in rows] == ["lc.write_file", "lc.search_docs"]
    assert rows[1]["status"] == "committed" and rows[1]["risk"] == "read"
    guard.undo(rows[0]["action"])
    assert files == {}


def test_mcp_annotations():
    assert risk_from_annotations({"readOnlyHint": True}, "anything") is Risk.READ
    assert risk_from_annotations({"destructiveHint": False}, "create_issue") is Risk.WRITE
    assert risk_from_annotations({}, "create_issue") is Risk.DESTRUCTIVE  # spec default
    assert risk_from_annotations({"destructiveHint": False, "openWorldHint": True}, "send_message") is Risk.IRREVERSIBLE
    assert risk_from_annotations(None, "get_issue") is Risk.READ


class FakeMCPSession:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name="get_issue", description="", annotations=SimpleNamespace(readOnlyHint=True)),
            SimpleNamespace(name="create_issue", description="",
                            annotations=SimpleNamespace(readOnlyHint=False, destructiveHint=False)),
            SimpleNamespace(name="delete_repo", description="", annotations=None),
        ])

    async def call_tool(self, name, arguments=None):
        await asyncio.sleep(0)
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": f"{name} ok"}]}

    async def initialize(self):
        return "ready"


def test_mcp_session_is_guarded(tmp_path):
    raw = FakeMCPSession()
    guard = Undolith(tmp_path / "u")
    guard.policy.add(Rule(verdict="deny", match="github.delete_*", reason="no repo deletion"))
    session = GuardedSession(guard, raw, server="github",
                             inverses={"create_issue": lambda a, s, r: raw.calls.append(("close_issue", a))})

    async def main():
        assert await session.initialize() == "ready"  # passthrough
        await session.list_tools()
        await session.call_tool("get_issue", {"n": 1})
        await session.call_tool("create_issue", {"title": "bug"})
        with pytest.raises(ActionDenied):
            await session.call_tool("delete_repo", {"repo": "x"})

    asyncio.run(main())
    assert raw.calls == [("get_issue", {"n": 1}), ("create_issue", {"title": "bug"})]
    guard.undo(guard.actions()[1]["action"])
    assert raw.calls[-1] == ("close_issue", {"title": "bug"})


def test_function_call_dispatch_explains_outcomes(tmp_path):
    guard = Undolith(tmp_path / "u")
    guard.policy.add(Rule(verdict="deny", match="bank.wipe"))

    @guard.tool("bank.transfer")
    def transfer(amount):
        return "done"

    @guard.tool("bank.wipe", risk="destructive")
    def wipe():
        return "gone"

    @guard.tool("bank.balance")
    def balance():
        return {"eur": 10}

    assert dispatch(guard, "balance", "{}", alias={"balance": "bank.balance"}) == '{"eur": 10}'
    assert "DENIED" in dispatch(guard, "bank.wipe", {})
    held = dispatch(guard, "bank.transfer", {"amount": 5})
    assert "HELD" in held and "NOT happened" in held
    assert isinstance(guard.call("bank.transfer", amount=1), Held)
