"""Verification / attestation: ``verify(action_id) -> proof`` and offline proof checking.

A proof is a self-contained JSON document:

* every signed ledger entry for the action,
* the signed chain head at the time the proof was made,
* optionally the full chain segment from the action's first entry to the head,
  which lets a verifier confirm nothing was spliced in or out,
* the signer's public key (Ed25519) and the claims derived from the entries.

A third party checks it with only the public key: ``verify_proof(proof, public_key=...)``.
Claims (status, authorized, ...) are recomputed from the entries, never trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from ._canon import utcnow
from .ledger import Ledger, check_entry
from .lifecycle import summarize
from .signing import Ed25519Verifier, Signer

PROOF_VERSION = 1


def build_proof(ledger: Ledger, action_id: str, *, segment: bool = False) -> Dict[str, Any]:
    from .model import UnknownAction

    entries = ledger.for_action(action_id)
    if not entries:
        raise UnknownAction(action_id)
    head = ledger.head()
    claims = summarize(entries)
    signer = ledger.signer
    proof: Dict[str, Any] = {
        "undolith_proof": PROOF_VERSION,
        "action": action_id,
        "qualname": f"{entries[0]['tool']}.{entries[0]['op']}",
        **claims,
        "entries": entries,
        "head": head,
        "signer": {
            "algorithm": signer.algorithm,
            "key_id": signer.key_id,
            "public_key": signer.public_key.hex() if signer.public_key else None,
        },
        "generated_at": utcnow(),
    }
    if segment:
        proof["segment"] = ledger.entries(since=entries[0]["seq"] - 1, until=head["seq"])
    return proof


@dataclass
class ProofCheck:
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    status: Optional[str] = None
    happened: Optional[bool] = None
    authorized: Optional[bool] = None

    def __bool__(self) -> bool:
        return self.valid


def verify_proof(proof: Dict[str, Any], *, public_key: Union[str, bytes, None] = None,
                 verifier: Optional[Signer] = None) -> ProofCheck:
    """Check a proof offline. Pin ``public_key`` to the key you already trust."""
    errors: List[str] = []
    warnings: List[str] = []
    if proof.get("undolith_proof") != PROOF_VERSION:
        return ProofCheck(False, [f"unsupported proof version {proof.get('undolith_proof')!r}"])

    signer_info = proof.get("signer") or {}
    if verifier is None:
        embedded = signer_info.get("public_key")
        if isinstance(public_key, str):
            public_key = bytes.fromhex(public_key)
        if public_key is not None:
            if embedded and bytes.fromhex(embedded) != public_key:
                errors.append("proof was signed by a different key than the one pinned")
            verifier = Ed25519Verifier(public_key)
        elif embedded and signer_info.get("algorithm") == "ed25519":
            verifier = Ed25519Verifier(bytes.fromhex(embedded))
            warnings.append("public key taken from the proof itself; pin it to prove *who* signed")
        else:
            return ProofCheck(False, ["no public key available (HMAC proofs need the secret: pass verifier=)"])

    action_id = proof.get("action")
    entries = proof.get("entries") or []
    if not entries:
        errors.append("proof has no entries")

    def check(e: Dict[str, Any], label: str) -> None:
        for p in check_entry(e, verifier):
            errors.append(f"{label} seq {e.get('seq')}: {p}")

    last_seq = 0
    for e in entries:
        check(e, "entry")
        if e.get("action") != action_id:
            errors.append(f"entry seq {e.get('seq')} belongs to another action")
        if e.get("seq", 0) <= last_seq:
            errors.append("entries are not in ledger order")
        last_seq = e.get("seq", 0)

    head = proof.get("head")
    if not head:
        errors.append("proof has no chain head")
    else:
        check(head, "head")
        if head.get("seq", 0) < last_seq:
            errors.append("head is older than the action's entries")

    segment = proof.get("segment")
    if segment is not None:
        by_seq = {e["seq"]: e for e in segment}
        for a, b in zip(segment, segment[1:]):
            if b.get("seq") != a.get("seq", 0) + 1 or b.get("prev") != a.get("hash"):
                errors.append(f"segment broken between seq {a.get('seq')} and {b.get('seq')}")
        for e in segment:
            check(e, "segment")
        for e in entries:
            if by_seq.get(e["seq"], {}).get("hash") != e.get("hash"):
                errors.append(f"entry seq {e['seq']} is not the one in the chain segment")
        if head and (not segment or segment[-1].get("hash") != head.get("hash")):
            errors.append("segment does not end at the chain head")
    else:
        warnings.append("no chain segment: entries are individually signed, but continuity is not shown")

    claims = summarize(entries) if entries else {}
    for key in ("status", "happened", "authorized"):
        if key in proof and claims.get(key) != proof[key]:
            errors.append(f"claimed {key}={proof[key]!r} but entries say {claims.get(key)!r}")

    return ProofCheck(not errors, errors, warnings, claims.get("status"), claims.get("happened"),
                      claims.get("authorized"))
