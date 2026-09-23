# Changelog

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
