import json
import sqlite3

import pytest

from undolith import ed25519
from undolith.ledger import GENESIS, Ledger
from undolith.signing import Ed25519Signer, Ed25519Verifier, HmacSigner, load_signer
from undolith.store import BlobStore, JsonlStore, SQLiteStore

# RFC 8032, section 7.1, tests 1 and 2
VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]


@pytest.mark.parametrize("sk,pk,msg,sig", VECTORS)
def test_pure_ed25519_matches_rfc8032(sk, pk, msg, sig):
    sk, pk, msg, sig = map(bytes.fromhex, (sk, pk, msg, sig))
    assert ed25519.public_key(sk) == pk
    assert ed25519.sign(sk, msg) == sig
    assert ed25519.verify(pk, msg, sig)
    assert not ed25519.verify(pk, msg + b"x", sig)
    assert not ed25519.verify(pk, msg, sig[:-1] + bytes([sig[-1] ^ 1]))


def test_signer_and_pure_verifier_agree():
    signer = Ed25519Signer(bytes(range(32)))
    sig = signer.sign(b"hello")
    assert ed25519.verify(signer.public_key, b"hello", sig)
    assert Ed25519Verifier(signer.public_key).verify(b"hello", sig)


def test_load_signer_persists_key(tmp_path):
    a = load_signer(tmp_path / "keys")
    b = load_signer(tmp_path / "keys")
    assert a.public_key == b.public_key
    assert (tmp_path / "keys" / "ed25519.pub").read_text().strip() == a.public_key.hex()
    h = load_signer(tmp_path / "keys", "hmac")
    assert h.verify(b"m", h.sign(b"m"))


@pytest.fixture(params=["sqlite", "jsonl"])
def ledger(request, tmp_path):
    store = SQLiteStore(tmp_path / "l.db") if request.param == "sqlite" else JsonlStore(tmp_path / "l.jsonl")
    yield Ledger(store, Ed25519Signer(b"k" * 32))
    store.close()


def test_chain_links_and_verifies(ledger):
    a = ledger.append("proposed", action="act_1", data={"x": 1})
    b = ledger.append("committed", action="act_1", data={"y": [1, 2]})
    assert a["prev"] == GENESIS and b["prev"] == a["hash"] and b["seq"] == 2
    report = ledger.verify_chain()
    assert report.ok and report.length == 2 and report.head == b["hash"]
    assert [e["kind"] for e in ledger.for_action("act_1")] == ["proposed", "committed"]


def test_wrong_key_is_detected(ledger):
    ledger.append("proposed", action="a")
    report = ledger.verify_chain(verifier=Ed25519Verifier(Ed25519Signer(b"z" * 32).public_key))
    assert not report.ok


def test_sqlite_is_append_only_at_db_level(tmp_path):
    store = SQLiteStore(tmp_path / "l.db")
    Ledger(store, HmacSigner(b"k")).append("proposed", action="a")
    conn = sqlite3.connect(tmp_path / "l.db")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE ledger SET kind='x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM ledger")
    conn.close()
    store.close()


def test_tampering_breaks_the_chain(tmp_path):
    store = SQLiteStore(tmp_path / "l.db")
    ledger = Ledger(store, Ed25519Signer(b"k" * 32))
    for i in range(4):
        ledger.append("committed", action=f"a{i}", data={"amount": i})
    conn = sqlite3.connect(tmp_path / "l.db", isolation_level=None)
    conn.execute("DROP TRIGGER ledger_no_update")  # a determined attacker with file access
    body = json.loads(conn.execute("SELECT body FROM ledger WHERE seq=2").fetchone()[0])
    body["data"]["amount"] = 1_000_000
    conn.execute("UPDATE ledger SET body=? WHERE seq=2", (json.dumps(body),))
    conn.close()
    report = ledger.verify_chain()
    assert not report.ok
    assert any(p["seq"] == 2 and "hash" in p["problem"] for p in report.problems)
    store.close()


def test_deleting_an_entry_breaks_the_chain(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger = Ledger(JsonlStore(path), HmacSigner(b"k"))
    for i in range(3):
        ledger.append("committed", action=f"a{i}")
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    report = ledger.verify_chain()
    assert not report.ok
    assert any("prev hash" in p["problem"] for p in report.problems)


def test_blob_store_detects_tampering(tmp_path):
    blobs = BlobStore(tmp_path / "b")
    ref = blobs.put_json({"a": 1})
    assert blobs.get_json(ref) == {"a": 1}
    (tmp_path / "b" / ref[:2] / ref[2:]).write_bytes(b'{"a":2}')
    with pytest.raises(Exception, match="modified"):
        blobs.get(ref)
