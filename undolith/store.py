"""Append-only entry stores and the content-addressed blob store.

Two ledger backends, both free and local:

* ``SQLiteStore`` - the default. Triggers make UPDATE/DELETE fail at the database
  level, and ``BEGIN IMMEDIATE`` serialises writers across processes.
* ``JsonlStore`` - one JSON object per line; greppable, diffable, single-writer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Union

from ._canon import canonical, sha256_hex
from .model import LedgerCorrupt

Entry = Dict
Builder = Callable[[Optional[Entry]], Entry]
Kinds = Union[str, Sequence[str], None]


def _kinds(kind: Kinds) -> Optional[set]:
    if kind is None:
        return None
    return {kind} if isinstance(kind, str) else set(kind)


class Store:
    def append(self, build: Builder) -> Entry:
        """Atomically: read the last entry, build the next one from it, persist it."""
        raise NotImplementedError

    def entries(self, *, action: str = None, session: str = None, kind: Kinds = None,
                since: int = 0, until: int = None) -> List[Entry]:
        raise NotImplementedError

    def last(self) -> Optional[Entry]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class SQLiteStore(Store):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS ledger (
        seq     INTEGER PRIMARY KEY,
        action  TEXT,
        session TEXT,
        kind    TEXT NOT NULL,
        hash    TEXT NOT NULL UNIQUE,
        body    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ledger_action  ON ledger(action);
    CREATE INDEX IF NOT EXISTS ledger_session ON ledger(session);
    CREATE INDEX IF NOT EXISTS ledger_kind    ON ledger(kind);
    CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
        BEGIN SELECT RAISE(ABORT, 'undolith ledger is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
        BEGIN SELECT RAISE(ABORT, 'undolith ledger is append-only'); END;
    """

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(self.SCHEMA)

    def append(self, build: Builder) -> Entry:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                entry = build(self._last())
                self._conn.execute(
                    "INSERT INTO ledger(seq, action, session, kind, hash, body) VALUES (?,?,?,?,?,?)",
                    (entry["seq"], entry.get("action"), entry.get("session"), entry["kind"],
                     entry["hash"], canonical(entry)),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            return entry

    def _last(self) -> Optional[Entry]:
        row = self._conn.execute("SELECT body FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def last(self) -> Optional[Entry]:
        with self._lock:
            return self._last()

    def entries(self, *, action=None, session=None, kind=None, since=0, until=None) -> List[Entry]:
        sql, params = ["SELECT body FROM ledger WHERE seq > ?"], [since]
        if until is not None:
            sql.append("AND seq <= ?")
            params.append(until)
        if action is not None:
            sql.append("AND action = ?")
            params.append(action)
        if session is not None:
            sql.append("AND session = ?")
            params.append(session)
        kinds = _kinds(kind)
        if kinds:
            sql.append(f"AND kind IN ({','.join('?' * len(kinds))})")
            params.extend(sorted(kinds))
        sql.append("ORDER BY seq")
        with self._lock:
            rows = self._conn.execute(" ".join(sql), params).fetchall()
        return [json.loads(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class JsonlStore(Store):
    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.touch(exist_ok=True)

    def _read(self) -> Iterator[Entry]:
        with open(self.path, "r", encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LedgerCorrupt(f"{self.path}:{n}: unreadable entry ({exc})") from None

    def append(self, build: Builder) -> Entry:
        with self._lock:
            entry = build(self._last())
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(canonical(entry) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return entry

    def _last(self) -> Optional[Entry]:
        last = None
        for last in self._read():
            pass
        return last

    def last(self) -> Optional[Entry]:
        with self._lock:
            return self._last()

    def entries(self, *, action=None, session=None, kind=None, since=0, until=None) -> List[Entry]:
        kinds = _kinds(kind)
        out = []
        with self._lock:
            for e in self._read():
                if e["seq"] <= since or (until is not None and e["seq"] > until):
                    continue
                if action is not None and e.get("action") != action:
                    continue
                if session is not None and e.get("session") != session:
                    continue
                if kinds and e["kind"] not in kinds:
                    continue
                out.append(e)
        return out


def open_store(home: Path, backend: str = "sqlite") -> Store:
    if backend == "sqlite":
        return SQLiteStore(home / "ledger.sqlite3")
    if backend == "jsonl":
        return JsonlStore(home / "ledger.jsonl")
    raise ValueError(f"unknown store backend {backend!r}; use 'sqlite' or 'jsonl'")


class BlobStore:
    """Content-addressed storage for arguments, snapshots and results.

    The ledger only carries each blob's SHA-256, so tampering with a blob is
    detected the moment it is read back.
    """

    def __init__(self, root: Union[str, Path]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, ref: str) -> Path:
        if len(ref) != 64 or any(c not in "0123456789abcdef" for c in ref):
            raise ValueError(f"bad blob ref {ref!r}")
        return self.root / ref[:2] / ref[2:]

    def put(self, data: bytes) -> str:
        ref = sha256_hex(data)
        path = self._path(ref)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return ref

    def get(self, ref: str) -> bytes:
        data = self._path(ref).read_bytes()
        if sha256_hex(data) != ref:
            raise LedgerCorrupt(f"blob {ref} was modified on disk")
        return data

    def put_json(self, obj) -> str:
        return self.put(canonical(obj).encode("utf-8"))

    def get_json(self, ref: str):
        return json.loads(self.get(ref).decode("utf-8"))

    def exists(self, ref: str) -> bool:
        return self._path(ref).exists()


def iter_refs(data) -> Iterable[str]:
    """All ``*_ref`` values inside an entry's data payload."""
    if isinstance(data, dict):
        for k, v in data.items():
            if k.endswith("_ref") and isinstance(v, str):
                yield v
            else:
                yield from iter_refs(v)
    elif isinstance(data, list):
        for v in data:
            yield from iter_refs(v)
