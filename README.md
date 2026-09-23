# Undolith

**Simulate → commit → undo for AI agent tool calls.**

Undolith sits between an agent and its tools. For every tool call it:

1. **classifies** the call as read, write, destructive or irreversible;
2. **checks a policy**, which can allow, simulate first, ask a human, hold, or deny;
3. **simulates** the call and shows a diff before anything changes;
4. **snapshots** the state it is about to change, then **commits**;
5. **compares** what happened with what the simulation predicted, and rolls back automatically if they differ;
6. **records** each step in a hash-chained, Ed25519-signed ledger;
7. can **undo** any action, a whole session, or everything after a checkpoint. It can also produce a **proof** that a third party checks with only a public key;
8. turns what went wrong into **regression tests**: a blocked, rejected or undone action becomes a test that the next version of your agent must pass ([below](#regression-tests-from-agent-traces)).

It needs no services, no API keys and no dependencies: Python 3.9+ standard library only. Its state is a local SQLite file.

```
agent ──tool call──▶ classify ─▶ policy ─▶ simulate ─▶ approve/hold ─▶ snapshot ─▶ commit ─▶ observe
                                   │                                                    │
                                  deny                               deviation? ─▶ rollback / halt
                                         every step ─▶ signed, hash-chained ledger ─▶ verify(action) → proof
```

> The protocol is written down in **[SPEC.md](SPEC.md)**. The code in this repository is its reference implementation.

---

## Why

Agents act in the real world: they delete files, run SQL, call APIs, send email and push code. When one gets it wrong, you need answers to three questions:

* **What exactly did it do?** Undolith keeps a tamper-evident, signed record of every call, including the ones that were denied or held.
* **Can we put it back?** Every reversible action has an inverse. Rollback runs newest-first and refuses to overwrite changes made since.
* **Can we prove it?** `verify(action_id)` returns a proof that someone else can check offline.

Irreversible actions like email, payments and `git push` cannot be undone, so Undolith holds them in an outbox until someone releases them.

## Install

```bash
pip install git+https://github.com/Nulfied/undolith
```

or clone the repository and run `pip install -e .`. If the optional `cryptography` package is installed, signing gets faster. Nothing else changes.

## 60-second tour

```python
from undolith import Undolith, Policy, Rule
from undolith.adapters import FileSystem, SQLiteDB, Email, file_transport

guard = Undolith(".undolith", policy=Policy(max_destructive_per_session=3))
guard.policy.add(Rule(verdict="deny", match="fs.*", args={"path": "*.env"}, reason="no secrets"))
guard.register(FileSystem("workspace"), SQLiteDB("app.db"), Email(file_transport("outbox")))

with guard.session(agent="planner") as s:
    s.call("fs.write", path="notes.md", content="# plan\n")           # diffed, snapshotted, committed
    s.call("db.execute", sql="DELETE FROM users WHERE inactive = 1")  # dry-run in a rolled-back txn first
    held = s.call("email.send", to="team@example.com", subject="done", body="…")  # irreversible → outbox

    s.rollback()        # undo everything this session did, newest first

guard.release(held.action_id, by="alice")   # or guard.discard(...)
proof = guard.verify(held.action_id, segment=True)
```

Run the demos. None of them needs keys or network access:

```bash
python examples/quickstart.py              # guard a function, prove it, roll it back
python examples/rogue_agent.py             # a "payroll bot" goes rogue: deny, blast radius, outbox, kill switch, proof
python examples/regression_from_ledger.py  # yesterday's incident becomes today's regression test
```

## Guarding your own tools

Use a decorator for any function:

```python
@guard.tool("tickets.create")            # risk inferred from the verb; override with risk="write"
def create_ticket(title: str) -> dict:
    return jira.create(title)

@create_ticket.inverse                   # how to undo it
def _(args, snapshot, result):
    jira.delete(result["id"])

@create_ticket.simulator                 # optional: what it will do, without doing it
def _(title):
    return Preview(f"create ticket {title!r}")
```

Or write a full adapter with `simulate`, `snapshot`, `undo` and `observe` hooks. The contract is in [SPEC.md §3](SPEC.md#3-the-adapter-contract). Hooks can be sync or `async`.

### Built-in adapters

| Adapter | Simulation | Undo |
|---|---|---|
| `FileSystem(root)` | unified diff of the change | restore previous bytes; sandboxed to `root` |
| `SQLiteDB(path)` | runs in a transaction that is rolled back; reports rows changed | online-backup snapshot, restored on undo |
| `HTTP(base_url)` | mock request preview | registered inverses (`POST /orders` → `DELETE /orders/{id}`); PUT/PATCH restored from a pre-GET |
| `Email(transport)` | rendered message | none: held in the outbox until released |
| `Shell()` | the tool's native dry-run (`git --dry-run`, `terraform plan`, `kubectl --dry-run`, `aws --dryrun`, `make -n`, …) | `git commit` → soft reset |

### Framework integrations

Undolith never imports these frameworks, so there is nothing extra to install.

```python
# LangChain: wraps tools in place
from undolith.integrations.langchain import guard_tools
tools = guard_tools(guard, tools, undo={"write_file": restore_fn})

# MCP: risk comes from the server's tool annotations (readOnlyHint / destructiveHint / openWorldHint)
from undolith.integrations.mcp import GuardedSession
session = GuardedSession(guard, client_session, server="github")
await session.list_tools()
await session.call_tool("create_issue", {"title": "…"})

# Any provider's tool-use loop (Anthropic, OpenAI, Ollama, …)
from undolith.integrations.function_calls import dispatch
output = dispatch(guard, block.name, block.input)   # denials, holds and rollbacks come back as text the model can read
```

## Regression tests from agent traces

Every blocked, rejected, undone or deviating action in the ledger is a failure that has *already been labelled*. `undolith.testgen` turns those failures into tests. For traces from elsewhere, a judge does the labelling: heuristics, or a small local model through [Ollama](https://ollama.com).

```python
from undolith.testgen import LiveTools, from_ledger, generate, run_suite

def agent(task, tools):                      # write the agent once, against tools.call(...)
    cfg = tools.call("fs.read", path="config.yaml")
    tools.call("fs.write", path="config.yaml", content=cfg.replace("workers: 4", "workers: 8"))
    return "Scaled to 8 workers."

with guard.session(task="Scale to 8 workers") as s:      # live: guarded, recorded, undoable
    s.finish(agent(s.task, LiveTools(s)))

suite, findings = generate(from_ledger(guard))           # golden tests + one regression test per failure
suite.save("agent_tests.json")
assert run_suite(suite, agent).ok                        # replay: recorded tool responses, no side effects
```

What gets generated:

| From | Test | Asserts |
|---|---|---|
| a clean run | **golden** | the same mutating calls, in order, and the same final answer |
| a denied / rejected / undone / discarded call | **regression** | the agent never makes that call again (paths generalised: `data/salaries.csv` → `data/*`) |
| a loop | **regression** | no identical call more than twice |
| a failure judged without a specific step | **regression** | the new run must pass the judge |

Tests replay recorded tool responses (like a VCR "cassette"), so they are deterministic and free, and safe to run in CI. Test ids are content hashes, so generating twice gives the same suite. Arguments come from the redacted ledger view, so secrets stay out of your test files.

**Public datasets and local judging, at $0:**

```bash
undolith testgen fetch-hf zai-org/AgentInstruct --split os --limit 50 -o os.jsonl   # AgentBench-derived trajectories
undolith testgen judge os.jsonl --judge both:llama3.2      # heuristics first, then a local model for the rest
undolith testgen import os.jsonl --judge both -o agent_tests.json
undolith testgen run agent_tests.json --agent my_agent:run
undolith testgen export-pytest agent_tests.json --agent my_agent:run -o tests/test_agent_regressions.py
```

Importers cover Undolith ledgers, OpenAI `tool_calls`, Anthropic `tool_use`, and ShareGPT/ReAct datasets (AgentInstruct's OS, DB, KG, ALFWorld, WebShop and Mind2Web formats). The Ollama judge **confirms its own failures** with a second prompt and downgrades unconfirmed ones to warnings. Small models produce false positives, and this pass filters many of them out.

Here is what it did on a first sample of 8 real AgentInstruct trajectories with `llama3.2` (3B) on a laptop CPU:

* It confirmed a real bug in a trajectory published as a gold example. `os_2` answers `0` to "how many entries have user-read permission" because it grepped the wrong column of `ls -l` (`'^...r'`).
* It filtered out its own 3 false positives on WebShop episodes that correctly end with `click[Buy Now]`.

That is a small sample and small models are noisy, so review flagged tests before you trust them. Every test records which judge and rule produced it. The format is specified in [SPEC.md §12](SPEC.md#12-traces--regression-tests).

## Policy

The defaults are `read → allow`, `write → simulate`, `destructive → simulate`, `irreversible → approve`, and then escalations apply:

* a destructive action with no inverse needs approval;
* more than *N* destructive actions in one session need approval (blast-radius limit);
* with `require_simulator`, anything whose effects could not be predicted needs approval.

Approvers are plain callables: `ConsoleApprover()`, `AutoApprove()`, `AutoReject()`, or your own Slack or web hook. An approver that returns `None` defers the decision, and the action waits in the outbox. Policies can be loaded from JSON; see [`examples/policy.json`](examples/policy.json).

## CLI

```bash
undolith log                         # one row per action
undolith show ACTION_ID              # all ledger entries for one action
undolith verify-chain                # every hash, link, signature and blob
undolith proof ACTION_ID --segment -o proof.json
undolith verify-proof proof.json --pubkey $(undolith pubkey)
undolith undo ACTION_ID
undolith rollback SESSION_ID
undolith kill --session SESSION_ID   # halt and roll back one agent
undolith kill                        # global kill switch for every process using this ledger
undolith held                        # outbox
undolith release ACTION_ID
undolith replay SESSION_ID --sandbox ./replay
undolith testgen from-ledger -o agent_tests.json          # ledger → regression tests
undolith testgen run agent_tests.json --agent my_agent:run
```

Commands that run inverses need your adapters. Point `--app mymodule:guard` at your `Undolith` instance.

## Guarantees and limits

* **Tamper evidence, not tamper-proofing.** Anyone who can write the files can damage the ledger, but `verify-chain` will show it. Without the signing key they cannot forge a valid chain. To defend against the key holder hiding entries, publish the chain head somewhere append-only from time to time.
* **Undolith only sees calls that go through it.** It is a seatbelt, not a sandbox. Use it together with OS-level isolation.
* **`.undolith/` holds full arguments and snapshots,** because undo and release need them. It gets its own `.gitignore`, and you should protect it like a secret store. Redacted keys (`password`, `token`, …) never appear in the ledger itself.
* **Whole-resource snapshots.** Undo restores the pre-state of what an action touched. Undo checks for conflicts before restoring, and rollback runs in reverse order, so an undo never silently overwrites newer changes.

## Development

```bash
pip install -e . pytest
pytest
```

The test suite covers the RFC 8032 test vectors, tamper detection, forged proofs, every adapter (with a local HTTP server and a real git repository), async tools, the LangChain, MCP and function-call integrations, and test generation end to end (including a fake Ollama server, so CI needs no model).

## Roadmap

- [ ] Chain-head anchoring (git notes / public transparency log)
- [ ] Postgres adapter (`SAVEPOINT`-based dry-run)
- [ ] S3 / cloud object-versioning adapter
- [ ] Web UI for the outbox and ledger
- [x] Regression tests generated from ledger traces (`undolith.testgen`)
- [ ] Trace minimisation: shrink a failing trace to the smallest reproducing prefix
- [ ] WebArena / OSWorld trajectory importers

## License

[MIT](LICENSE) © Nulfied
