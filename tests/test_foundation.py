"""common.py: canonical JSON, Ed25519 sign/verify, tamper detection."""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
import sys  # noqa: E402

sys.path.insert(0, str(REPO))

import common  # noqa: E402


def test_canonical_json_is_key_order_independent():
    a = {"z": 1, "a": 2, "m": {"y": 1, "x": 2}}
    b = {"a": 2, "m": {"x": 2, "y": 1}, "z": 1}
    assert common.canonical_json(a) == common.canonical_json(b)


def test_tx_hash_is_deterministic_and_sensitive_to_amount():
    h1 = common.compute_tx_hash("user1", "addr2", 6000, "USD", "transfer_high")
    h2 = common.compute_tx_hash("user1", "addr2", 6000, "USD", "transfer_high")
    h3 = common.compute_tx_hash("user1", "addr2", 6001, "USD", "transfer_high")
    assert h1 == h2
    assert h1 != h3


def test_sign_and_verify_roundtrip(tmp_path):
    sk, pk = common.generate_keypair()
    obj = {"a": 1, "b": "x"}
    sig = common.sign_object(sk, obj)
    signed = {**obj, "signature": sig}
    assert common.verify_object(pk, signed, sig) is True


def test_verify_fails_on_tampered_payload():
    sk, pk = common.generate_keypair()
    obj = {"amount": 100}
    sig = common.sign_object(sk, obj)
    tampered = {"amount": 999, "signature": sig}
    assert common.verify_object(pk, tampered, sig) is False


def test_verify_fails_on_tampered_signature():
    sk, pk = common.generate_keypair()
    obj = {"amount": 100}
    sig = common.sign_object(sk, obj)
    bad_sig = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    assert common.verify_object(pk, {**obj, "signature": sig}, bad_sig) is False


def test_verify_fails_with_wrong_public_key():
    sk1, _ = common.generate_keypair()
    _, pk2 = common.generate_keypair()
    obj = {"amount": 100}
    sig = common.sign_object(sk1, obj)
    assert common.verify_object(pk2, {**obj, "signature": sig}, sig) is False
