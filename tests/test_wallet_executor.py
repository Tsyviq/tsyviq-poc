"""Wallet/Executor: verify_rod ordering, atomic commit, replay/expiry.

T9-T12 are unit tests of verify_rod() directly, not HTTP round trips.
Reason: Event Source always derives action_id/tx_hash/event_type/request_id
on an issued ROD from its OWN pending-request record (A.5 grabli — "берутся
только из server-side pending request"), and Wallet always fetches the ROD
for an intent via that intent's own pending_request_id. There is no
reachable path in this architecture where those fields could organically
mismatch at the HTTP layer — which is exactly the point (constitutive
closure). verify_rod()'s field checks are the defense-in-depth backstop for
"what if a mismatched ROD reached this function anyway", and that is
what T9-T12 test directly, using build_signed_rod() (see conftest.py) to
produce a genuinely-signed ROD with one deliberately wrong field.
"""

import threading

import pytest

from .conftest import build_signed_rod, prepare_and_acquire_high, wallet_mod


def test_T1_execute_without_rod_is_not_constituted(wallet):
    wallet.client.post("/demo/prepare", params={"stakes": "high"})
    r = wallet.client.post("/demo/execute-no-rod").json()
    assert r == {
        "action_id": r["action_id"],
        "constituted": False,
        "status": "NOT_CONSTITUTED",
        "reason": "no pending event request for this action",
        "balance": None,
        "cq": None,
    }
    assert wallet.client.get("/balance/user1").json()["balance"] == 10000


def test_T2_forged_rod_via_illustration_endpoint_is_rejected(wallet):
    wallet.client.post("/demo/prepare", params={"stakes": "high"})
    r = wallet.client.post("/demo/forge-attempt").json()
    assert r["cq_formed"] is False
    assert r["constituted"] is False
    assert "signature" in r["reason"]
    assert wallet.client.get("/balance/user1").json()["balance"] == 10000


def test_T2_verify_rod_rejects_invalid_signature(event_pk):
    import common as common_mod

    intent = _base_intent()
    # signed by a different (attacker) keypair, not the real event_sk
    bad_sk, _ = common_mod.generate_keypair()
    rod = build_signed_rod(
        bad_sk, request_id="req-1", action_id="a1", tx_hash=intent.tx_hash, event_type="usb_insert"
    )
    ok, reason = wallet_mod.verify_rod(intent, _base_policy(), rod, event_pk, set(), set())
    assert ok is False
    assert "signature" in reason


def _tx_hash():
    import common as common_mod

    return common_mod.compute_tx_hash("user1", "addr2", 6000, "USD", "transfer_high")


def _base_intent():
    return wallet_mod.Intent(
        action_id="a1", frm="user1", to="addr2", amount=6000, currency="USD",
        action_type="transfer_high", tx_hash=_tx_hash(), pending_request_id="req-1",
    )


def _base_policy():
    return {"requires_event": True, "event_type": "usb_insert", "max_age_sec": 90}


def test_T9_rod_action_id_mismatch_is_rejected(event_sk, event_pk):
    intent = _base_intent()
    rod = build_signed_rod(event_sk, request_id="req-1", action_id="OTHER-ACTION", tx_hash=intent.tx_hash, event_type="usb_insert")
    ok, reason = wallet_mod.verify_rod(intent, _base_policy(), rod, event_pk, set(), set())
    assert ok is False and "action_id" in reason


def test_T10_rod_tx_hash_mismatch_is_rejected(event_sk, event_pk):
    intent = _base_intent()
    rod = build_signed_rod(event_sk, request_id="req-1", action_id="a1", tx_hash="0" * 64, event_type="usb_insert")
    ok, reason = wallet_mod.verify_rod(intent, _base_policy(), rod, event_pk, set(), set())
    assert ok is False and "tx_hash" in reason


def test_T11_rod_wrong_event_type_is_rejected(event_sk, event_pk):
    intent = _base_intent()
    rod = build_signed_rod(event_sk, request_id="req-1", action_id="a1", tx_hash=intent.tx_hash, event_type="nfc_tap")
    ok, reason = wallet_mod.verify_rod(intent, _base_policy(), rod, event_pk, set(), set())
    assert ok is False and "event_type" in reason


def test_T12_expired_rod_is_rejected(event_sk, event_pk):
    import common as common_mod
    from datetime import timedelta

    intent = _base_intent()
    old_ts = (common_mod.now_utc() - timedelta(seconds=200)).isoformat()
    rod = build_signed_rod(
        event_sk, request_id="req-1", action_id="a1", tx_hash=intent.tx_hash, event_type="usb_insert", acquired_at=old_ts
    )
    ok, reason = wallet_mod.verify_rod(intent, _base_policy(), rod, event_pk, set(), set())
    assert ok is False and "expired" in reason


def test_rod_request_id_mismatch_is_rejected(event_sk, event_pk):
    intent = _base_intent()  # pending_request_id="req-1"
    rod = build_signed_rod(event_sk, request_id="req-DIFFERENT", action_id="a1", tx_hash=intent.tx_hash, event_type="usb_insert")
    ok, reason = wallet_mod.verify_rod(intent, _base_policy(), rod, event_pk, set(), set())
    assert ok is False and "request_id" in reason


def test_rod_replay_is_rejected_by_consumed_sets(event_sk, event_pk):
    intent = _base_intent()
    rod = build_signed_rod(event_sk, request_id="req-1", action_id="a1", tx_hash=intent.tx_hash, event_type="usb_insert")
    consumed_rods = {rod["rod_id"]}
    ok, reason = wallet_mod.verify_rod(intent, _base_policy(), rod, event_pk, consumed_rods, set())
    assert ok is False and "consumed" in reason


def test_happy_path_prepare_request_insert_execute(wallet):
    intent = prepare_and_acquire_high(wallet)
    assert intent["amount"] == 6000
    r = wallet.client.post("/demo/execute").json()
    assert r["constituted"] is True
    assert r["status"] == "EXECUTED"
    assert r["balance"] == 4000
    assert r["cq"]["action_id"] == intent["action_id"]
    assert wallet.client.get("/balance/user1").json()["balance"] == 4000


def test_T13_replay_execute_does_not_double_spend(wallet):
    prepare_and_acquire_high(wallet)
    r1 = wallet.client.post("/demo/execute").json()
    assert r1["balance"] == 4000
    r2 = wallet.client.post("/demo/execute").json()
    assert r2["constituted"] is True
    assert r2["balance"] == 4000
    assert wallet.client.get("/balance/user1").json()["balance"] == 4000


def test_T14_concurrent_double_execute_changes_balance_once(wallet):
    prepare_and_acquire_high(wallet)
    action_id = wallet.state.demo_action_id

    results = []

    def do_execute():
        r = wallet.client.post("/tx/execute", json={"action_id": action_id})
        results.append(r.json())

    threads = [threading.Thread(target=do_execute) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r["constituted"] is True for r in results)
    assert wallet.client.get("/balance/user1").json()["balance"] == 4000
    assert len(wallet.state.consumed_rod_ids) == 1


def test_T15_low_stakes_executes_without_any_rod(wallet):
    wallet.client.post("/demo/prepare", params={"stakes": "low"})
    r = wallet.client.post("/demo/execute").json()
    assert r["constituted"] is True
    assert r["status"] == "EXECUTED"
    assert r["balance"] == 9900


def test_demo_reset_restores_initial_state(wallet):
    prepare_and_acquire_high(wallet)
    wallet.client.post("/demo/execute")
    assert wallet.client.get("/balance/user1").json()["balance"] == 4000

    r = wallet.client.post("/demo/reset").json()
    assert r["balance"] == 10000
    assert wallet.client.get("/balance/user1").json()["balance"] == 10000
    assert wallet.client.get("/demo/state").json() == {"action_id": None}
