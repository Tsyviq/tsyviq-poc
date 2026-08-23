"""T17: sweep every reachable public/demo endpoint without ever acquiring a
qualifying ROD, confirm none of them causes a high-stakes commit.

T18: caller-supplied ROD-shaped fields (fake origin_commitment, forged
signature, chosen rod_id) smuggled into request bodies are ignored — no
endpoint in this system accepts a ROD from a caller in the first place, so
there is nothing for such fields to bypass.
"""


def test_T17_constitutive_closure_no_endpoint_commits_without_acquired_rod(wallet):
    intent = wallet.client.post("/demo/prepare", params={"stakes": "high"}).json()
    action_id = intent["action_id"]

    # Every reachable Wallet path that could conceivably touch consequential
    # state — deliberately never call mock.simulate_insert(), so no ROD is
    # ever acquired anywhere in this test.
    wallet.client.post("/tx/execute", json={"action_id": action_id})
    wallet.client.post("/demo/execute-no-rod")
    wallet.client.post("/demo/execute")
    wallet.client.post("/demo/forge-attempt")
    wallet.client.post("/demo/request-event")  # creates a pending request only — still BOD, not a commit
    wallet.client.post("/demo/execute")
    wallet.client.post("/tx/execute", json={"action_id": action_id})

    assert wallet.client.get("/balance/user1").json()["balance"] == 10000
    assert wallet.state.intents[action_id].status == "prepared"
    assert len(wallet.state.consumed_rod_ids) == 0


def test_T17_low_stakes_path_is_unaffected_by_the_sweep(wallet):
    # Sanity check that closure holds specifically for the high-stakes path,
    # not because execute is broken outright — low-stakes still works.
    wallet.client.post("/demo/prepare", params={"stakes": "low"})
    r = wallet.client.post("/demo/execute").json()
    assert r["constituted"] is True and r["balance"] == 9900


def test_T18_fake_fields_in_event_request_body_are_ignored(event_source):
    r = event_source.client.post(
        "/event/request",
        json={
            "action_id": "a1",
            "tx_hash": "h1",
            "event_type": "usb_insert",
            "origin_commitment": "deadbeef" * 8,
            "signature": "forged-signature",
            "rod_id": "attacker-chosen-id",
            "device_id": "demo:serial:FORGED",
            "acquired_at": "2020-01-01T00:00:00+00:00",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "awaiting_usb_insert"
    assert "rod" not in body

    s = event_source.client.get(f"/event/status/{body['request_id']}").json()
    assert s["status"] == "awaiting_usb_insert"
    assert s["rod"] is None


def test_T18_wallet_execute_has_no_field_for_a_caller_supplied_rod(wallet):
    wallet.client.post("/demo/prepare", params={"stakes": "high"})
    action_id = wallet.state.demo_action_id
    r = wallet.client.post(
        "/tx/execute",
        json={
            "action_id": action_id,
            "rod": {"origin_commitment": "a" * 64, "signature": "fake", "rod_id": "attacker"},
        },
    ).json()
    assert r["constituted"] is False
    assert r["status"] == "NOT_CONSTITUTED"
    assert wallet.client.get("/balance/user1").json()["balance"] == 10000
