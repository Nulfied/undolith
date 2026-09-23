"""Entry signers.

* ``Ed25519Signer`` (default): anyone holding the public key can verify a proof.
* ``HmacSigner``: faster, but only holders of the secret can verify.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Optional

from . import ed25519

try:  # optional speed-up; the pure-Python path is always available
    from cryptography.exceptions import InvalidSignature as _InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey as _PrivKey,
        Ed25519PublicKey as _PubKey,
    )
except Exception:  # pragma: no cover - depends on environment
    _PrivKey = _PubKey = None


def _fast_verify(public: bytes, msg: bytes, sig: bytes) -> bool:
    if _PubKey is None:
        return ed25519.verify(public, msg, sig)
    try:
        _PubKey.from_public_bytes(public).verify(sig, msg)
        return True
    except (_InvalidSignature, ValueError):
        return False


class Signer:
    algorithm: str = ""
    key_id: str = ""
    public_key: Optional[bytes] = None

    def sign(self, msg: bytes) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def verify(self, msg: bytes, sig: bytes) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class Ed25519Signer(Signer):
    algorithm = "ed25519"

    def __init__(self, secret: bytes):
        self._secret = secret
        self._fast = _PrivKey.from_private_bytes(secret) if _PrivKey is not None else None
        self.public_key = ed25519.public_key(secret)
        self.key_id = key_id_for(self.public_key)

    def sign(self, msg: bytes) -> bytes:
        if self._fast is not None:
            return self._fast.sign(msg)
        return ed25519.sign(self._secret, msg, self.public_key)

    def verify(self, msg: bytes, sig: bytes) -> bool:
        return _fast_verify(self.public_key, msg, sig)


class Ed25519Verifier(Signer):
    """Verify-only view of an Ed25519 key (what a third party holds)."""

    algorithm = "ed25519"

    def __init__(self, public_key: bytes):
        self.public_key = public_key
        self.key_id = key_id_for(public_key)

    def sign(self, msg: bytes) -> bytes:
        raise PermissionError("verify-only key")

    def verify(self, msg: bytes, sig: bytes) -> bool:
        return _fast_verify(self.public_key, msg, sig)


class HmacSigner(Signer):
    algorithm = "hmac-sha256"

    def __init__(self, key: bytes):
        self._key = key
        self.key_id = "hmac:" + hashlib.sha256(b"undolith-key-id:" + key).hexdigest()[:16]

    def sign(self, msg: bytes) -> bytes:
        return hmac.new(self._key, msg, hashlib.sha256).digest()

    def verify(self, msg: bytes, sig: bytes) -> bool:
        return hmac.compare_digest(self.sign(msg), sig)


def key_id_for(public_key: bytes) -> str:
    return "ed25519:" + hashlib.sha256(public_key).hexdigest()[:16]


def _read_or_create(path: Path) -> bytes:
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secret.hex())
    return secret


def load_signer(keys_dir: Path | str, algorithm: str = "ed25519") -> Signer:
    """Load the local signing key, generating one on first use."""
    keys_dir = Path(keys_dir)
    if algorithm == "ed25519":
        signer = Ed25519Signer(_read_or_create(keys_dir / "ed25519.key"))
        (keys_dir / "ed25519.pub").write_text(signer.public_key.hex() + "\n")
        return signer
    if algorithm in ("hmac", "hmac-sha256"):
        return HmacSigner(_read_or_create(keys_dir / "hmac.key"))
    raise ValueError(f"unknown signing algorithm {algorithm!r}")
