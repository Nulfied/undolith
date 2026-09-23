"""Derive an action's state from its ledger entries.

The ledger is the only source of truth: status is always recomputed from
entries, never stored separately. That is what lets a third party check a
proof's claims instead of trusting them.

    proposed ─┬─ denied
              ├─ simulated ─┬─ approved ─┐
              │             ├─ rejected  │
              │             └─ held ─┬─ released ─┐
              │                      └─ discarded │
              └──────────────────────────────────┴─ prepared ─┬─ committed ─┬─ (deviation)
                                                              └─ failed     └─ undone
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

STATUS_BY_KIND = {
    "read": "committed",
    "proposed": "proposed",
    "denied": "denied",
    "rejected": "rejected",
    "held": "held",
    "released": "released",
    "discarded": "discarded",
    "prepared": "in_flight",
    "committed": "committed",
    "failed": "failed",
    "undone": "undone",
}
UNDOABLE = ("committed", "failed", "in_flight")


def first(entries: List[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
    return next((e for e in entries if e["kind"] == kind), None)


def last(entries: List[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
    return next((e for e in reversed(entries) if e["kind"] == kind), None)


def summarize(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    status, happened, authorized, authorization, deviated = "unknown", False, False, None, False
    for e in entries:
        kind, data = e["kind"], e.get("data") or {}
        status = STATUS_BY_KIND.get(kind, status)
        if kind == "read":
            happened, authorized = True, True
            authorization = {"by": "policy", "verdict": "allow"}
        elif kind in ("proposed", "escalated"):
            verdict = data.get("verdict")
            authorized = verdict in ("allow", "simulate")
            authorization = {"by": "policy", "verdict": verdict, "reason": data.get("reason"),
                             "policy": data.get("policy")}
        elif kind in ("approved", "released"):
            authorized = True
            authorization = {"by": data.get("by"), "via": "approval" if kind == "approved" else "release",
                             "note": data.get("note", ""), "entry": e["seq"]}
        elif kind in ("denied", "rejected", "discarded"):
            authorized = False
        elif kind == "committed":
            happened = True
        elif kind == "deviation":
            deviated = True
    return {"status": status, "happened": happened, "authorized": authorized,
            "authorization": authorization, "deviated": deviated}
