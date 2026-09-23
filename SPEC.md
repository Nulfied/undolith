# The Undolith protocol: simulate → commit → undo

**Status:** draft v0.1 · **Ledger format:** v1 · **Proof format:** v1

This document specifies how an agent's side effects are intercepted, predicted,
executed, recorded, proven and reversed. The Python package in this repository
is the reference implementation, but nothing here is Python-specific. Another
implementation that follows this spec should produce ledgers and proofs that
Undolith can verify, and the other way round.

The key words MUST, SHOULD and MAY are used as in RFC 2119.

---

## 1. Goals and non-goals

**Goals**

1. No action with side effects runs until a policy has looked at it.
2. Before a mutating action runs, its effects are predicted where possible and shown as a diff.
3. Every action leaves a tamper-evident, signed record, including actions that were denied or held.
4. Every reversible action can be undone later from the record alone, by anyone with the adapter.
5. A third party can check what happened, and whether it was authorized, from a proof file and a public key.
6. Zero cost: local files only, no services, no dependencies.

**Non-goals**

* Isolating the agent from the host. Undolith guards the calls that go through it and nothing else. Run it together with a sandbox, not in place of one.
* Undoing effects in the physical world. Irreversible actions are held back until someone decides, not undone afterwards.
* Distributed consensus. The ledger has one writer (one signing key). Proofs let others check that writer's claims.

---

## 2. Concepts

| Term | Meaning |
|---|---|
| **Operation** | A named capability `tool.op` (e.g. `fs.write`) plus its hooks (§3). |
| **Action** | One invocation of an operation with concrete arguments. Identified by an `action_id`. |
| **Session** | A run of one agent. The scope for blast-radius counters, rollback and the kill switch. |
| **Risk** | `read` < `write` < `destructive` < `irreversible` (§4). |
| **Verdict** | `allow` < `simulate` < `approve` < `hold` < `deny` (§5). |
| **Preview** | The output of a simulation: `summary`, `diff`, `predicted` post-state, `simulated` flag. |
| **Snapshot** | The pre-state captured immediately before commit. It is what undo restores. |
| **Ledger** | Append-only, hash-chained, signed entries (§7). |
| **Blob** | Content-addressed (SHA-256) storage for arguments, snapshots, diffs and results. |

---

## 3. The adapter contract

An operation is a `run` function plus up to six optional hooks. Every hook MAY be async.

```
run(**args)                     -> result          the real side effect
simulate(**args)                -> Preview         MUST NOT have side effects
snapshot(**args)                -> JSON            pre-state needed to undo; MUST be JSON-serialisable
undo(args, snapshot, result)    -> None            the inverse
observe(args, result)           -> {key: value}    observable post-state
reversible(args)                -> bool            per-call reversibility (default: undo exists)
risk: Risk | callable(args) -> Risk
```

The rules that make these compose:

* **`simulate` MUST be pure.** Use a native dry-run (`git --dry-run`, `terraform plan`,
  a DB transaction that is rolled back), a mock that describes the request, or a
  model of the tool. If effects cannot be predicted, return `simulated=false`.
* **`predicted` and `observe` share keys.** After commit, Undolith compares
  `preview.predicted[k]` with `observe(...)[k]` for every key present in both (§6.4).
* **`observe` describes state, not events.** It is called twice: after commit,
  and again right before undo. If the two results differ, someone changed the
  world in between, and undo MUST refuse unless forced (§8.2).
* **`undo` receives the JSON round-tripped `result`,** not the live object. So
  inverses MUST depend only on JSON-able data (e.g. `result["id"]`).
* **Snapshots are taken after approval and immediately before `run`.** A snapshot
  taken at simulation time would be stale by commit time.

---

## 4. Risk classification

| Risk | Meaning | Examples |
|---|---|---|
| `read` | No side effects | `fs.read`, `GET`, `SELECT`, `git status` |
| `write` | Side effects that can normally be undone | create/overwrite a file, `INSERT`, `POST` with an inverse |
| `destructive` | Removes or overwrites data | delete, `DROP`, `git clean`, `DELETE` |
| `irreversible` | Leaves the system; cannot be recalled | send email, payment, publish, `git push`, deploy |

Risk comes from, in order of precedence:

1. an explicit override from the integrator,
2. the operation's declared `risk` (which MAY depend on the arguments, e.g. SQL verb or HTTP method),
3. framework metadata (MCP `readOnlyHint` / `destructiveHint` / `openWorldHint`),
4. a name heuristic on the first verb token (`get_*` → read, `delete_*` → destructive, `send_*` → irreversible).

Unknown operations MUST default to at least `write`. The heuristic never lowers a declared risk.

---

## 5. Policy

A policy maps each action to a verdict:

1. **Rules**, first match wins. A rule matches on a glob of `tool.op`, a risk range,
   argument globs (`{"path": "*.env"}`), and optionally a predicate.
2. **Defaults** per risk when no rule matches:
   `read → allow`, `write → simulate`, `destructive → simulate`, `irreversible → approve`.
3. `deny` is final. `read` actions are never escalated.
4. **Escalations** only ever make the verdict stricter:
   * `destructive`+ and not reversible → at least `approve`
   * blast radius: more than *N* destructive actions (or *M* mutations) in the session → at least `approve`
   * `require_simulator` and the preview came back `simulated=false` → `approve`, recorded as an `escalated` entry

| Verdict | Simulate | Ask approver | Commit |
|---|---|---|---|
| `allow` | no | no | immediately |
| `simulate` | yes | no | automatically after preview |
| `approve` | yes | yes (sync) | if approved. With no approver, or if the approver returns `null`, becomes `hold` |
| `hold` | yes | no | only when released from the outbox |
| `deny` | no | no | never |

The **policy fingerprint** is `sha256(canonical(policy.to_dict()))`. It is recorded
with every `proposed` entry, so a proof shows which rules were in force.

The JSON form is in [`examples/policy.json`](examples/policy.json).

---

## 6. Lifecycle

### 6.1 States

```
proposed ─┬─ denied
          ├─ simulated ─┬─ [escalated] ─┐
          │             ├─ approved ────┤
          │             ├─ rejected     │
          │             └─ held ─┬─ released ─┐
          │                      └─ discarded │
          └─────────────────────────────────┴─ prepared ─┬─ committed ─┬─ [deviation]
                                                           └─ failed    └─ undone | undo_failed
```

Reads that the policy allows are recorded as a single `read` entry.

An action's status is **always derived from its entries** (the last status-bearing
kind wins). It is never stored separately. That is what lets a verifier recompute
the claims in a proof (§9).

### 6.2 Pipeline

For each call:

1. **Classify** risk (§4). Check the kill switch: if it is engaged, raise `SessionHalted`.
2. **Decide** the verdict (§5). Append `proposed {risk, verdict, reason, reversible, args (redacted), args_ref, policy}`.
3. `deny` → append `denied` and stop.
4. Unless the verdict is `allow`: **simulate** and append `simulated {summary, predicted, simulated, details, diff_ref, diff_excerpt}`.
5. `approve` → ask the approver. Append `approved` or `rejected` with `{by, note}`.
   `hold` (or `approve` with no decision) → append `held {reason}` and return a `Held` handle.
6. **Snapshot** and append `prepared {snapshot_ref}`. This is a write-ahead record:
   it is durable *before* the side effect happens, so a crash during `run` can be
   recovered by undoing the in-flight action.
7. **Run.** On an exception, append `failed {error}` and re-raise.
8. **Observe** and append `committed {result_ref, result_sha256, result_preview, observed}`.
9. **Compare** prediction and observation (§6.4).

### 6.3 Held actions (outbox)

`release(action_id, by)` appends `released {by, note}` and then continues from
step 6, using the stored arguments. `discard(action_id, by, reason)` appends
`discarded`. A held action never commits without a `released` entry naming who
released it.

### 6.4 Deviation

For every key in both `predicted` and `observed`:

```
score = |{k : canonical(predicted[k]) ≠ canonical(observed[k])}| / |compared keys|
```

If `score > threshold` (default `0.0`, meaning any mismatch counts), append
`deviation {mismatches, score, response}` and apply the configured response:

| `on_deviation` | Effect |
|---|---|
| `warn` | Record only |
| `rollback` (default) | Undo this action, raise `DeviationDetected` |
| `halt` | Halt the session without undoing, raise |
| `rollback_session` | Halt the session and roll all of it back, raise |

---

## 7. Ledger format (v1)

Each entry is a JSON object:

```json
{
  "v": 1,
  "seq": 42,
  "ts": "2026-09-23T10:15:00.123456Z",
  "kind": "committed",
  "action": "act_01a0cf1d8624a37ae8e082",
  "session": "ses_01a0cf1d85f2c1d0b7a9e1",
  "agent": "payroll-bot",
  "tool": "fs",
  "op": "delete",
  "data": { "...": "kind-specific payload, see §6.2" },
  "prev": "<hash of entry 41, or 64 zeros for entry 1>",
  "key": "ed25519:3f9a0c1b2d4e5f60",
  "hash": "<sha256 hex>",
  "sig": "<signature hex>"
}
```

* **Canonical JSON:** keys sorted, separators `,` and `:`, UTF-8, no ASCII escaping.
  Bytes become `{"$b64": ...}`. Values that cannot be serialised become `{"$repr": ...}`.
* **`hash`** = `sha256(canonical({v, seq, ts, kind, action, session, agent, tool, op, data, prev, key}))`.
* **`sig`** = `Sign(key, ascii(hash))`. It is Ed25519 (RFC 8032) by default. HMAC-SHA256 is allowed
  for private deployments, but then only holders of the secret can verify.
* **`key`** = `"ed25519:" + sha256(public_key)[:16]` (or `"hmac:" + ...`).
* `seq` starts at 1 and goes up by exactly 1. `prev` of entry *n* MUST equal `hash` of entry *n−1*.
* Session-level entries (`rollback`, `halted`, `resumed`) have `action = null`.
  Global kill-switch entries have `session = null` as well.

**Chain verification** walks every entry and checks: `seq` continuity, `prev`
linkage, hash recomputation, the signature, and optionally that every `*_ref`
blob exists and matches its hash.

**Storage.** SQLite (default) with `UPDATE`/`DELETE` triggers that abort, and
`BEGIN IMMEDIATE` so writers from several processes are serialised. JSONL is
also available (one entry per line, single writer). Blobs live at
`blobs/<sha[:2]>/<sha[2:]>` and are re-hashed on every read.

**Redaction.** Argument keys that match the policy's `redact` list (`password`,
`token`, `api_key`, …) appear in the ledger as `"[redacted sha256:…]"`. The full
arguments are kept in a blob (`args_ref`), because release and replay need
them. So the `.undolith/` directory is sensitive: it is git-ignored
automatically and SHOULD be protected like any secret store.

---

## 8. Undo

### 8.1 Single action

`undo(action_id)` is allowed when the status is `committed`, `failed` (partial
effects) or `in_flight` (crash after `prepared`). It:

1. loads `args` (from `args_ref`), `snapshot` (from `snapshot_ref`) and `result` (from `result_ref`),
2. runs the precondition check (§8.2),
3. calls `undo(args, snapshot, result)`,
4. appends `undone {by, reason, forced}`, or `undo_failed {error}` and re-raises.

Undo is idempotent: undoing an `undone` action returns `false` and has no effect.
Operations with no inverse raise `NotReversible`.

### 8.2 Precondition: no clobbering

If the operation has `observe` and the `committed` entry recorded `observed`,
then `observe(args, result)` is called again before undo. If the current state
differs from the recorded post-state, undo MUST raise `UndoConflict` unless
`force=true`. This stops "undo my edit from an hour ago" from wiping out
everything that happened since.

### 8.3 Session rollback

`rollback(session, to=seq)` undoes every undoable action whose `prepared` entry
is after `to`, in **reverse commit order**. Reverse order is what keeps the
precondition check satisfied for repeated edits to the same resource.
Irreversible actions are reported as `skipped` and failures as `failed`, and
rollback continues past both. The whole run is recorded as a `rollback` entry.

`Session.batch()` takes a checkpoint and rolls back to it if the block raises. That gives all-or-nothing multi-step plans.

### 8.4 Kill switch

* **Session:** append `halted`. Every later call in that session raises `SessionHalted`.
  By default the session is also rolled back.
* **Global:** create `<home>/KILL` and append a global `halted`. Every call in every
  process sharing that home halts until the file is removed (`resume`).

---

## 9. Proofs

`verify(action_id, segment?) → proof`:

```json
{
  "undolith_proof": 1,
  "action": "act_…",
  "qualname": "fs.delete",
  "status": "undone",
  "happened": true,
  "authorized": true,
  "authorization": {"by": "policy", "verdict": "simulate", "policy": "<fingerprint>"},
  "deviated": false,
  "entries": ["every ledger entry for the action"],
  "head": "the chain head entry when the proof was generated",
  "segment": ["optional: every entry from the action's first entry to head"],
  "signer": {"algorithm": "ed25519", "key_id": "ed25519:…", "public_key": "<hex>"},
  "generated_at": "…"
}
```

A verifier with only the public key MUST check that:

1. the public key it trusts matches the embedded one, when the verifier pinned a key;
2. every entry, the head, and every segment entry has a correct hash and a valid signature;
3. every entry belongs to this action, in increasing `seq`, and the head is not older than them;
4. if a segment is present: it is contiguous (`seq+1`, `prev` = previous `hash`), it contains
   exactly these entries, and it ends at the head;
5. the claims `status`, `happened` and `authorized` equal the values recomputed from the entries (§6.1).

`authorized` is true when the policy verdict was `allow` or `simulate`, or when an
`approved` or `released` entry exists (the proof names who). It is false for
`deny`, `reject` and `discard`, and while an action is still waiting in the outbox.

**What a proof shows:** the key holder recorded this sequence of events for this
action, and did not change them afterwards without the change being detectable.
**What it does not show:** that the key holder recorded *every* action. Nothing
local can show that. Publish the chain head (e.g. in a git commit or a
transparency log) from time to time to anchor it.

---

## 10. Replay

`replay(session, into)` re-runs a session's committed actions, in order, inside
another Undolith (normally a sandbox with a permissive policy). It compares each
result's content hash with the original, and undoes in the sandbox the actions
that were undone in the original. Use it to audit, reproduce or bisect agent
behaviour.

---

## 11. Security considerations

* **Signing keys** live in `<home>/keys/`, created with mode `0600`. Anyone with the key can write valid entries.
* **The pure-Python Ed25519 is not constant-time.** If `cryptography` is installed, it is used automatically.
* **Adapters are trusted code.** A malicious `simulate` can have side effects. Review adapters like any other dependency.
* **The filesystem adapter** resolves every path and refuses to act outside its root. Symlinks are resolved before the check.
* **The shell adapter** takes argv lists (no shell), so arguments cannot be interpreted as extra shell commands.
* **Framework hints are advisory.** MCP annotations come from the server. Pin risks you care about with explicit overrides or rules.
