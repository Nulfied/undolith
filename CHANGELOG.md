# Changelog

## 0.2.0 (2026-09-23)

Agent traces → regression tests (`undolith.testgen`, [SPEC.md §12](SPEC.md#12-traces--regression-tests)).

* Importers: Undolith ledgers, OpenAI `tool_calls`, Anthropic `tool_use`, and ShareGPT/ReAct datasets (AgentInstruct / AgentBench: OS, DB, KG, ALFWorld, WebShop, Mind2Web). `fetch-hf` reads public Hugging Face datasets through the free datasets-server API.
* Judges: deterministic heuristics (ledger statuses, loops, tool errors, give-ups), a local Ollama judge with a self-confirmation pass that filters false positives, and a composite judge that asks the model only when heuristics are inconclusive.
* Generator: golden tests from clean runs, regression tests from each failure (forbidden calls with generalised paths, repeat limits, judge checks), content-hashed ids, merging, secrets kept out.
* Runner: `ReplayTools` cassettes (deterministic, no side effects), `LiveTools` to run the same agent live through Undolith, `run_suite`, and pytest export.
* CLI: `undolith testgen from-ledger | import | judge | fetch-hf | run | export-pytest | show`.
* Core: `session(task=...)` and `Session.finish(answer, outcome=...)` record `started` / `finished` entries. Read results are now kept (`result_ref`) so reads can be replayed.
* New demo: `examples/regression_from_ledger.py`.

## 0.1.0 (2026-09-23)

First public release.

* Protocol spec: [SPEC.md](SPEC.md) (lifecycle, ledger format v1, proof format v1).
* Gateway: classify → policy → simulate → approve/hold → snapshot → commit → observe → compare.
* Hash-chained, Ed25519-signed ledger (pure-Python RFC 8032; uses `cryptography` if installed). SQLite with append-only triggers, or JSONL.
* Policy engine: rules with argument globs, per-risk defaults, blast-radius limits, escalation, JSON config, fingerprinting.
* Undo, session rollback, checkpoints, all-or-nothing batches, conflict detection, session and global kill switch.
* Deviation detection with warn, halt, rollback and rollback-session responses.
* Outbox for irreversible actions: hold, release, discard, approver deferral.
* `verify(action_id)` proofs with an optional chain segment, and offline `verify_proof`.
* Replay of sessions into a sandbox.
* Adapters: filesystem, SQLite, HTTP, email, shell (native dry-runs).
* Integrations: `@guard.tool` decorator, LangChain, MCP (annotation-aware), provider-agnostic function-call dispatch.
* CLI: `log`, `show`, `sessions`, `verify-chain`, `proof`, `verify-proof`, `pubkey`, `undo`, `rollback`, `kill`, `resume`, `held`, `release`, `discard`, `replay`, `policy`.
