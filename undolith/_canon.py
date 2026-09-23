"""Canonical JSON, hashing, ids and timestamps.

Everything that ends up in the ledger goes through :func:`canonical` so that the
same logical value always produces the same bytes (and therefore the same hash).
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import enum
import hashlib
import json
import secrets
import time
from pathlib import PurePath
from typing import Any


def _default(o: Any) -> Any:
    if isinstance(o, (bytes, bytearray, memoryview)):
        return {"$b64": base64.b64encode(bytes(o)).decode("ascii")}
    if isinstance(o, enum.Enum):
        return o.value
    if isinstance(o, PurePath):
        return str(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=repr)
    if isinstance(o, (_dt.datetime, _dt.date, _dt.time)):
        return o.isoformat()
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    dump = getattr(o, "model_dump", None)  # pydantic v2 (MCP / LangChain results)
    if callable(dump):
        try:
            return dump(mode="json")
        except Exception:  # pragma: no cover - defensive
            pass
    return {"$repr": repr(o)}


def canonical(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8, lossy-but-stable fallbacks."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def jsonable(obj: Any) -> Any:
    """Round-trip through canonical JSON so the value is plain JSON data."""
    return json.loads(canonical(obj))


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def digest(obj: Any) -> str:
    """Content hash of any JSON-able value."""
    return sha256_hex(canonical(obj))


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    """Time-ordered, collision-resistant id, e.g. ``act_0192f3c4a1b2c3d4e5f6a7``."""
    return f"{prefix}_{time.time_ns() // 1_000_000:012x}{secrets.token_hex(5)}"


def short(obj: Any, limit: int = 160) -> str:
    text = obj if isinstance(obj, str) else canonical(obj)
    return text if len(text) <= limit else text[: limit - 1] + "…"
