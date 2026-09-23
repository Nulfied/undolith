"""Filesystem adapter: every write is diffed before and fully reversible after.

All paths are confined to ``root``; anything that resolves outside it is refused.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..model import Preview, Risk
from ..ops import Adapter, Operation


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode(data: Optional[bytes]) -> Optional[str]:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class FileSystem(Adapter):
    tool = "fs"

    def __init__(self, root: Union[str, Path] = ".", *, max_diff_lines: int = 400):
        self.root = Path(root).resolve()
        self.max_diff_lines = max_diff_lines

    # -- helpers -----------------------------------------------------------------
    def resolve(self, path: Union[str, Path]) -> Path:
        p = (self.root / path).resolve()
        if p != self.root and self.root not in p.parents:
            raise PermissionError(f"{path!s} escapes the sandbox root {self.root}")
        return p

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() if p != self.root else "."

    def _state(self, p: Path) -> Dict[str, Any]:
        if not p.is_file():
            return {"exists": p.exists(), "sha256": None}
        return {"exists": True, "sha256": _sha(p.read_bytes())}

    def _capture(self, p: Path) -> Dict[str, Any]:
        if p.is_file():
            return {"path": str(p), "existed": True, "data": base64.b64encode(p.read_bytes()).decode("ascii")}
        return {"path": str(p), "existed": p.exists(), "data": None, "is_dir": p.is_dir()}

    @staticmethod
    def _restore(snap: Dict[str, Any]) -> None:
        p = Path(snap["path"])
        if snap.get("data") is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(snap["data"]))
        elif not snap.get("existed") and p.is_file():
            p.unlink()

    def _diff(self, rel: str, old: Optional[bytes], new: Optional[bytes]) -> str:
        a, b = _decode(old), _decode(new)
        if (old is not None and a is None) or (new is not None and b is None):
            return f"Binary file {rel}: {len(old or b'')} -> {len(new or b'')} bytes\n"
        lines = list(difflib.unified_diff((a or "").splitlines(True), (b or "").splitlines(True),
                                          fromfile=f"a/{rel}" if old is not None else "/dev/null",
                                          tofile=f"b/{rel}" if new is not None else "/dev/null"))
        if len(lines) > self.max_diff_lines:
            lines = lines[: self.max_diff_lines] + [f"... ({len(lines) - self.max_diff_lines} more diff lines)\n"]
        return "".join(l if l.endswith("\n") else l + "\n" for l in lines)

    @staticmethod
    def _bytes(content: Union[str, bytes], encoding: str) -> bytes:
        return content if isinstance(content, bytes) else content.encode(encoding)

    # -- operations --------------------------------------------------------------
    def read(self, path: str, encoding: Optional[str] = "utf-8") -> Union[str, bytes]:
        data = self.resolve(path).read_bytes()
        return data.decode(encoding) if encoding else data

    def list(self, path: str = ".") -> List[str]:
        p = self.resolve(path)
        return sorted(self._rel(c) + ("/" if c.is_dir() else "") for c in p.iterdir())

    def exists(self, path: str) -> bool:
        return self.resolve(path).exists()

    def write(self, path: str, content: Union[str, bytes], encoding: str = "utf-8") -> Dict[str, Any]:
        p, data = self.resolve(path), self._bytes(content, encoding)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return {"path": self._rel(p), "bytes": len(data)}

    def append(self, path: str, content: Union[str, bytes], encoding: str = "utf-8") -> Dict[str, Any]:
        p, data = self.resolve(path), self._bytes(content, encoding)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "ab") as fh:
            fh.write(data)
        return {"path": self._rel(p), "bytes": len(data)}

    def delete(self, path: str) -> Dict[str, Any]:
        p = self.resolve(path)
        if p.is_dir():
            raise IsADirectoryError(f"{path}: fs.delete removes files; use fs.rmdir for empty directories")
        size = p.stat().st_size
        p.unlink()
        return {"path": self._rel(p), "bytes": size}

    def move(self, src: str, dst: str) -> Dict[str, Any]:
        s, d = self.resolve(src), self.resolve(dst)
        if not s.is_file():
            raise FileNotFoundError(src)
        d.parent.mkdir(parents=True, exist_ok=True)
        os.replace(s, d)
        return {"src": self._rel(s), "dst": self._rel(d)}

    def mkdir(self, path: str) -> Dict[str, Any]:
        p = self.resolve(path)
        p.mkdir(parents=True, exist_ok=True)
        return {"path": self._rel(p)}

    def rmdir(self, path: str) -> Dict[str, Any]:
        p = self.resolve(path)
        p.rmdir()  # only empty directories: recursive deletes are not reversible here by design
        return {"path": self._rel(p)}

    # -- simulations -------------------------------------------------------------
    def _sim_write(self, path, content, encoding="utf-8", *, append=False) -> Preview:
        p = self.resolve(path)
        old = p.read_bytes() if p.is_file() else None
        new = (old or b"") + self._bytes(content, encoding) if append else self._bytes(content, encoding)
        rel = self._rel(p)
        verb = "create" if old is None else ("append to" if append else "overwrite")
        return Preview(summary=f"{verb} {rel} ({len(old or b'')} -> {len(new)} bytes)",
                       diff=self._diff(rel, old, new), predicted={"exists": True, "sha256": _sha(new)})

    def _sim_delete(self, path) -> Preview:
        p = self.resolve(path)
        if not p.is_file():
            return Preview(summary=f"delete {path}: no such file (will fail)", predicted={"exists": p.exists()})
        old = p.read_bytes()
        rel = self._rel(p)
        return Preview(summary=f"delete {rel} ({len(old)} bytes)", diff=self._diff(rel, old, None),
                       predicted={"exists": False, "sha256": None})

    def _sim_move(self, src, dst) -> Preview:
        s, d = self.resolve(src), self.resolve(dst)
        clobber = " (overwrites existing file)" if d.exists() else ""
        data = s.read_bytes() if s.is_file() else None
        return Preview(summary=f"move {self._rel(s)} -> {self._rel(d)}{clobber}",
                       predicted={"src_exists": False, "dst_sha256": _sha(data) if data is not None else None})

    # -- operation table ---------------------------------------------------------
    def operations(self) -> List[Operation]:
        state = lambda args, result: self._state(self.resolve(args["path"]))  # noqa: E731
        snap = lambda path, **_: self._capture(self.resolve(path))  # noqa: E731
        restore = lambda args, s, result: self._restore(s)  # noqa: E731

        def move_snapshot(src, dst):
            return {"src": self._capture(self.resolve(src)), "dst": self._capture(self.resolve(dst))}

        def move_undo(args, s, result):
            d = self.resolve(args["dst"])
            src_path = Path(s["src"]["path"])
            src_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(d, src_path)
            self._restore(s["dst"])

        def move_observe(args, result):
            s, d = self.resolve(args["src"]), self.resolve(args["dst"])
            return {"src_exists": s.exists(), "dst_sha256": _sha(d.read_bytes()) if d.is_file() else None}

        def mkdir_snapshot(path):
            p, missing = self.resolve(path), []
            while p != self.root and not p.exists():
                missing.append(str(p))
                p = p.parent
            return {"created": missing}

        def mkdir_undo(args, s, result):
            for d in s["created"]:  # deepest first
                try:
                    Path(d).rmdir()
                except OSError:
                    pass

        def move_risk(args):
            return Risk.DESTRUCTIVE if self.resolve(args["dst"]).exists() else Risk.WRITE

        return [
            self.op("read", self.read, risk=Risk.READ),
            self.op("list", self.list, risk=Risk.READ),
            self.op("exists", self.exists, risk=Risk.READ),
            self.op("write", self.write, risk=Risk.WRITE, simulate=self._sim_write, snapshot=snap,
                    undo=restore, observe=state, description="Create or overwrite a file"),
            self.op("append", self.append, risk=Risk.WRITE,
                    simulate=lambda path, content, encoding="utf-8": self._sim_write(path, content, encoding, append=True),
                    snapshot=snap, undo=restore, observe=state),
            self.op("delete", self.delete, risk=Risk.DESTRUCTIVE, simulate=self._sim_delete, snapshot=snap,
                    undo=restore, observe=state),
            self.op("move", self.move, risk=move_risk, simulate=self._sim_move, snapshot=move_snapshot,
                    undo=move_undo, observe=move_observe),
            self.op("mkdir", self.mkdir, risk=Risk.WRITE, snapshot=mkdir_snapshot, undo=mkdir_undo,
                    simulate=lambda path: Preview(summary=f"mkdir {path}", predicted={"is_dir": True}),
                    observe=lambda args, r: {"is_dir": self.resolve(args["path"]).is_dir()}),
            self.op("rmdir", self.rmdir, risk=Risk.WRITE,
                    simulate=lambda path: Preview(summary=f"remove empty directory {path}", predicted={"is_dir": False}),
                    snapshot=lambda path: {"path": str(self.resolve(path))},
                    undo=lambda args, s, r: Path(s["path"]).mkdir(parents=True, exist_ok=True),
                    observe=lambda args, r: {"is_dir": self.resolve(args["path"]).is_dir()}),
        ]

