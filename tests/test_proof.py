import copy
import json

from undolith import verify_proof
from undolith.signing import Ed25519Signer, HmacSigner


def make(guard):
    guard.call("fs.write", path="p.txt", content="1")
    guard.call("fs.write", path="q.txt", content="2")
    return guard.actions()[0]["action"]


def test_proof_verifies_offline_with_pinned_key(guard):
    aid = make(guard)
    proof = json.loads(json.dumps(guard.verify(aid, segment=True)))  # as if sent over the wire
    check = verify_proof(proof, public_key=guard.signer.public_key.hex())
    assert check.valid, check.errors
    assert check.status == "committed" and check.happened and check.authorized
    assert not check.warnings


def test_unpinned_proof_warns(guard):
    check = verify_proof(guard.verify(make(guard)))
    assert check.valid
    assert any("pin" in w for w in check.warnings) and any("segment" in w for w in check.warnings)


def test_forged_claims_are_caught(guard):
    proof = guard.verify(make(guard))
    proof["authorized"] = True
    proof["status"] = "undone"
    check = verify_proof(proof, public_key=guard.signer.public_key)
    assert not check.valid and any("claimed status" in e for e in check.errors)


def test_tampered_entry_is_caught(guard):
    proof = guard.verify(make(guard), segment=True)
    bad = copy.deepcopy(proof)
    bad["entries"][0]["data"]["args"]["content"] = "evil"
    assert not verify_proof(bad, public_key=guard.signer.public_key)


def test_wrong_key_is_caught(guard):
    proof = guard.verify(make(guard))
    other = Ed25519Signer(b"o" * 32).public_key
    check = verify_proof(proof, public_key=other)
    assert not check.valid and any("different key" in e for e in check.errors)


def test_segment_splice_is_caught(guard):
    proof = guard.verify(make(guard), segment=True)
    del proof["segment"][1]
    check = verify_proof(proof, public_key=guard.signer.public_key)
    assert not check.valid and any("segment broken" in e for e in check.errors)


def test_undone_action_proof(guard):
    aid = make(guard)
    guard.rollback(guard.actions()[0]["session"])
    check = verify_proof(guard.verify(aid), public_key=guard.signer.public_key)
    assert check.valid and check.status == "undone" and check.happened


def test_hmac_proof_needs_secret(tmp_path):
    from undolith import Undolith
    from undolith.adapters import FileSystem

    ws = tmp_path / "w"
    ws.mkdir()
    signer = HmacSigner(b"secret")
    g = Undolith(tmp_path / "u", signer=signer).register(FileSystem(ws))
    g.call("fs.write", path="a", content="b")
    proof = g.verify(g.actions()[0]["action"])
    assert not verify_proof(proof)
    assert verify_proof(proof, verifier=signer)
