"""SQLite adapter: native dry-run via a rolled-back transaction, undo via a snapshot.

Simulation runs the statement inside a transaction, measures what changed, and
rolls back. Before a real commit the database is copied with SQLite's online
backup API; undo restores that copy. Restores are whole-database, so roll back
newest-first (``Session.rollback`` does) - the undo conflict check stops you
from restoring over changes made after the action.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Sequence, Union

from ..model import Preview, Risk
from ..ops import Adapter, Operation

_VERB = re.compile(r"^\s*(?:--[^\n]*\n\s*)*(\w+)", re.IGNORECASE)
_READ = {"select", "explain", "pragma", "with"}
_DESTRUCTIVE = {"delete", "drop", "truncate", "alter", "replace"}


def sql_risk(sql: str) -> Risk:
    m = _VERB.match(sql or "")
    verb = m.group(1).lower() if m else ""
    if verb in _READ:
        return Risk.READ if not re.search(r"\b(insert|update|delete|drop|alter|create)\b", sql, re.I) else Risk.WRITE
    if verb in _DESTRUCTIVE:
        return Risk.DESTRUCTIVE
    return Risk.WRITE


class SQLiteDB(Adapter):
    tool = "db"

    def __init__(self, path: Union[str, Path], *, max_snapshot_bytes: int = 64 * 1024 * 1024):
        self.path = Path(path).resolve()
        self.max_snapshot_bytes = max_snapshot_bytes

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _counts(self, conn: sqlite3.Connection) -> Dict[str, int]:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}

    def _fingerprint(self) -> str:
        conn = self._connect()
        try:
            h = hashlib.sha256()
            for (sql,) in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"):
                h.update(sql.encode())
            for name in sorted(self._counts(conn)):
                for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
                    h.update(repr(tuple(row)).encode())
            return h.hexdigest()
        finally:
            conn.close()

    # -- operations --------------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(sql, tuple(params))]
        finally:
            conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            before = conn.total_changes
            cur = conn.execute(sql, tuple(params))
            changes = conn.total_changes - before
            conn.execute("COMMIT")
            return {"rowcount": cur.rowcount, "lastrowid": cur.lastrowid, "changes": changes}
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def simulate(self, sql: str, params: Sequence[Any] = ()) -> Preview:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            counts_before = self._counts(conn)
            before = conn.total_changes
            conn.execute(sql, tuple(params))
            changes = conn.total_changes - before
            counts_after = self._counts(conn)
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()
        deltas = {t: counts_after.get(t, 0) - counts_before.get(t, 0)
                  for t in set(counts_before) | set(counts_after)
                  if counts_after.get(t, 0) != counts_before.get(t, 0)}
        summary = f"{changes} row(s) affected" + (f"; row counts {deltas}" if deltas else "")
        return Preview(summary=summary, predicted={"changes": changes},
                       details={"row_count_deltas": deltas, "method": "transaction rolled back"})

    def snapshot(self, sql: str = "", params: Sequence[Any] = ()) -> Dict[str, Any]:
        if self.path.exists() and self.path.stat().st_size > self.max_snapshot_bytes:
            raise ValueError(f"{self.path} is larger than max_snapshot_bytes; raise it or use a different undo")
        fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        try:
            src, dst = sqlite3.connect(str(self.path)), sqlite3.connect(tmp)
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            return {"path": str(self.path), "db": base64.b64encode(Path(tmp).read_bytes()).decode("ascii")}
        finally:
            os.unlink(tmp)

    def restore(self, args: Dict[str, Any], snap: Dict[str, Any], result: Any) -> None:
        fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        try:
            Path(tmp).write_bytes(base64.b64decode(snap["db"]))
            src, dst = sqlite3.connect(tmp), sqlite3.connect(snap["path"])
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
        finally:
            os.unlink(tmp)

    def observe(self, args: Dict[str, Any], result: Any) -> Dict[str, Any]:
        return {"changes": (result or {}).get("changes"), "db_sha256": self._fingerprint()}

    def operations(self) -> List[Operation]:
        return [
            self.op("query", self.query, risk=Risk.READ),
            self.op("execute", self.execute, risk=lambda a: sql_risk(a.get("sql", "")), simulate=self.simulate,
                    snapshot=self.snapshot, undo=self.restore, observe=self.observe),
        ]
