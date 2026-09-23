"""Heuristic risk classification for tools that do not declare their own risk.

The rule is deliberately conservative: anything not recognisably read-only is
at least WRITE, and an unknown verb never lowers the risk.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

from .model import Risk

READ_VERBS = {
    "get", "list", "ls", "read", "cat", "search", "find", "fetch", "query", "describe", "show", "head",
    "stat", "view", "count", "exists", "lookup", "inspect", "status", "diff", "log", "peek", "browse",
    "select", "check", "validate", "preview", "explain", "summarize", "retrieve", "load", "info", "whoami",
}
DESTRUCTIVE_VERBS = {
    "delete", "del", "remove", "rm", "rmdir", "drop", "destroy", "truncate", "purge", "wipe", "erase",
    "kill", "terminate", "reset", "clean", "clear", "overwrite", "revoke", "unlink", "uninstall", "prune",
    "archive", "cancel", "disable", "deactivate", "force",
}
IRREVERSIBLE_VERBS = {
    "send", "email", "mail", "sms", "text", "notify", "pay", "purchase", "buy", "order", "checkout",
    "transfer", "charge", "refund", "withdraw", "wire", "trade", "sell", "publish", "tweet", "broadcast",
    "deploy", "release", "push", "invite", "sign", "submit", "post_message", "dispatch", "execute_trade",
}

_SPLIT = re.compile(r"[^a-zA-Z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def tokens(name: str) -> List[str]:
    return [t.lower() for t in _SPLIT.split(name or "") if t]


def classify_name(name: str, description: str = "") -> Risk:
    """Guess a risk level from a tool/function name like ``send_email`` or ``getUser``."""
    toks = tokens(name)
    if not toks:
        return Risk.WRITE
    joined = "_".join(toks)
    verb = toks[0]
    if verb in READ_VERBS:
        return Risk.READ
    if verb in IRREVERSIBLE_VERBS or joined in IRREVERSIBLE_VERBS:
        return Risk.IRREVERSIBLE
    if verb in DESTRUCTIVE_VERBS:
        return Risk.DESTRUCTIVE
    rest = set(toks[1:])
    if rest & IRREVERSIBLE_VERBS:
        return Risk.IRREVERSIBLE
    if rest & DESTRUCTIVE_VERBS:
        return Risk.DESTRUCTIVE
    desc = set(tokens(description))
    if {"irreversible", "payment", "sends"} & desc:
        return Risk.IRREVERSIBLE
    if {"permanently", "deletes", "destructive"} & desc:
        return Risk.DESTRUCTIVE
    return Risk.WRITE


def max_risk(risks: Iterable[Optional[Risk]]) -> Risk:
    found = [r for r in risks if r is not None]
    return max(found) if found else Risk.WRITE
