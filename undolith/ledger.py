"""The hash-chained, signed action ledger.

Every entry commits to the previous entry's hash, so editing, deleting or
reordering anything breaks the chain from that point on. Every entry hash is
signed, so an attacker who rewrites the whole chain still cannot forge it
without the signing key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ._canon import canonical, jsonable, sha256_hex, utcnow
from .signing import Signer
from .store import BlobStore, Store, iter_refs

GENESIS = "0" * 64
FORMAT_VERSION = 1
HASHED_FIELDS = ("v", "seq", "ts", "kind", "action", "session", "agent", "tool", "op", "data", "prev", "key")


def entry_hash(entry: Dict[str, Any]) -> str:
    return sha256_hex(canonical({k: entry.get(k) for k in HASHED_FIELDS}))


def check_entry(entry: Dict[str, Any], verifier: Optional[Signer]) -> List[str]:
    """Problems with a single entry (empty list means it is intact)."""
    problems = []
    if entry_hash(entry) != entry.get("hash"):
        problems.append("hash does not match contents")
    if verifier is not None:
        if entry.get("key") != verifier.key_id:
            problems.append(f"signed by unexpected key {entry.get('key')}")
        else:
            try:
                ok = verifier.verify(entry["hash"].encode("ascii"), bytes.fromhex(entry.get("sig", "")))
            except (ValueError, KeyError):
                ok = False
            if not ok:
                problems.append("bad signature")
    return problems


@dataclass
class ChainReport:
    ok: bool
    length: int
    head: Optional[str]
    problems: List[Dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


class Ledger:
    def __init__(self, store: Store, signer: Signer):
        self.store = store
        self.signer = signer

    def append(self, kind: str, *, action: str = None, session: str = None, agent: str = None,
               tool: str = None, op: str = None, data: Dict[str, Any] = None) -> Dict[str, Any]:
        payload = jsonable(data or {})

        def build(last):
            entry = {
                "v": FORMAT_VERSION,
                "seq": last["seq"] + 1 if last else 1,
                "ts": utcnow(),
                "kind": kind,
                "action": action,
                "session": session,
                "agent": agent,
                "tool": tool,
                "op": op,
                "data": payload,
                "prev": last["hash"] if last else GENESIS,
                "key": self.signer.key_id,
            }
            entry["hash"] = entry_hash(entry)
            entry["sig"] = self.signer.sign(entry["hash"].encode("ascii")).hex()
            return entry

        return self.store.append(build)

    def entries(self, **filters) -> List[Dict[str, Any]]:
        return self.store.entries(**filters)

    def for_action(self, action_id: str) -> List[Dict[str, Any]]:
        return self.store.entries(action=action_id)

    def head(self) -> Optional[Dict[str, Any]]:
        return self.store.last()

    def verify_chain(self, verifier: Optional[Signer] = None, blobs: Optional[BlobStore] = None) -> ChainReport:
        """Walk the whole chain: hashes, links, signatures and (optionally) referenced blobs."""
        verifier = verifier or self.signer
        prev, n, problems, head = GENESIS, 0, [], None
        for entry in self.store.entries():
            n += 1
            seq = entry.get("seq")
            if seq != n:
                problems.append({"seq": seq, "problem": f"expected seq {n} (entry missing or reordered)"})
            if entry.get("prev") != prev:
                problems.append({"seq": seq, "problem": "prev hash does not link to the previous entry"})
            for p in check_entry(entry, verifier):
                problems.append({"seq": seq, "problem": p})
            if blobs is not None:
                for ref in iter_refs(entry.get("data")):
                    try:
                        blobs.get(ref)
                    except FileNotFoundError:
                        problems.append({"seq": seq, "problem": f"blob {ref[:12]}… missing"})
                    except Exception as exc:
                        problems.append({"seq": seq, "problem": str(exc)})
            prev = entry.get("hash")
            head = prev
        return ChainReport(ok=not problems, length=n, head=head, problems=problems)
