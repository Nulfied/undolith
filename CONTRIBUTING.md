# Contributing

Undolith is built in the open. Issues, adapters, and spec discussions are all welcome.

* **Keep the core dependency-free.** Everything under `undolith/` must run on the Python standard library alone. Optional speed-ups (like `cryptography`) are imported only if they are already installed.
* **Adapters follow [SPEC.md §3](SPEC.md#3-the-adapter-contract).** `simulate` must have no side effects, `observe` describes state rather than events, and inverses depend only on JSON data.
* **Behaviour changes to the lifecycle, ledger or proof format need a SPEC.md change in the same PR.**
* **Tests:** `pip install -e . pytest && pytest`. New adapters need a round-trip test (commit → undo → state matches) and a simulation-has-no-side-effects test.
