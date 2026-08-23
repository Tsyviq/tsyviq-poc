"""Shared primitives for all three TSYVIQ PoC services.

Imported directly (same repo, same venv, same machine) by wallet-simulator,
policy-service and event-source, so canonical JSON encoding and signing can
never diverge between Event Source (signer) and Executor (verifier).
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


# ---------------------------------------------------------------------------
# Canonical JSON
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding: sorted keys, compact separators, UTF-8.

    Both the signer (Event Source) and the verifier (Wallet/Executor) call
    this exact function, so there is no risk of divergent canonicalization
    between the two sides.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# IDs and timestamps
# ---------------------------------------------------------------------------


def new_uuid() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_utc_iso() -> str:
    return now_utc().isoformat()


def parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_seconds(ts: str) -> float:
    return (now_utc() - parse_iso(ts)).total_seconds()


# ---------------------------------------------------------------------------
# tx_hash — essential transfer parameters
# ---------------------------------------------------------------------------


def compute_tx_hash(
    frm: str, to: str, amount: int, currency: str, action_type: str
) -> str:
    payload = {
        "frm": frm,
        "to": to,
        "amount": amount,
        "currency": currency,
        "action_type": action_type,
    }
    return sha256_hex(canonical_json(payload))


# ---------------------------------------------------------------------------
# Ed25519 keypair generation / (de)serialization
# ---------------------------------------------------------------------------


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    sk = Ed25519PrivateKey.generate()
    return sk, sk.public_key()


def private_key_to_pem(sk: Ed25519PrivateKey) -> bytes:
    return sk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key_to_pem(pk: Ed25519PublicKey) -> bytes:
    return pk.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def load_private_key_pem(path: Path) -> Ed25519PrivateKey:
    data = Path(path).read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} does not contain an Ed25519 private key")
    return key


def load_public_key_pem(path: Path) -> Ed25519PublicKey:
    data = Path(path).read_bytes()
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"{path} does not contain an Ed25519 public key")
    return key


# ---------------------------------------------------------------------------
# Signing / verification of raw bytes
# ---------------------------------------------------------------------------


def sign_bytes(sk: Ed25519PrivateKey, data: bytes) -> bytes:
    return sk.sign(data)


def verify_bytes(pk: Ed25519PublicKey, data: bytes, signature: bytes) -> bool:
    try:
        pk.verify(signature, data)
        return True
    except InvalidSignature:
        return False


# ---------------------------------------------------------------------------
# Signing / verification of JSON objects (used for ROD)
# ---------------------------------------------------------------------------


def sign_object(
    sk: Ed25519PrivateKey, obj: Mapping[str, Any], exclude: tuple[str, ...] = ("signature",)
) -> str:
    """Sign canonical JSON of obj minus `exclude` fields. Returns base64 signature."""
    payload = {k: v for k, v in obj.items() if k not in exclude}
    sig = sign_bytes(sk, canonical_json(payload))
    return base64.b64encode(sig).decode("ascii")


def verify_object(
    pk: Ed25519PublicKey,
    obj: Mapping[str, Any],
    signature_b64: str,
    exclude: tuple[str, ...] = ("signature",),
) -> bool:
    try:
        sig = base64.b64decode(signature_b64)
    except Exception:
        return False
    payload = {k: v for k, v in obj.items() if k not in exclude}
    return verify_bytes(pk, canonical_json(payload), sig)
