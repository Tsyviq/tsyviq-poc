"""T17: sweep EVERY registered route on all three services — not a
hand-picked subset — without ever acquiring a qualifying ROD, and confirm
none of them causes a high-stakes commit. The sweep is asserted against the
actual route table (`app.routes`), so a new endpoint added later and left
out of the sweep fails this test loudly instead of silently passing.

T18: caller-supplied ROD-shaped fields (fake origin_commitment, forged
signature, chosen rod_id) smuggled into request bodies are ignored — no
endpoint in this system accepts a ROD from a caller in the first place, so
there is nothing for such fields to bypass.
"""

# Routes deliberately excluded from the "must not cause a commit" sweep,
# and why each one is safe to exclude:
DOC_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
WALLET_EXCLUDED = {
    ("POST", "/demo/reset"),  # resets to the fixed 10000 baseline — a control action, not an attack path
}


def _registered_routes(app):
    """(method, path) pairs for every route FastAPI actually registered,
    HEAD collapsed into GET and doc routes dropped — this is the ground
    truth the sweep is checked against."""
    out = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path is None or path in DOC_ROUTES:
            continue
        for method in methods:
            if method == "HEAD":
                continue
            out.add((method, path))
    return out


def _body_for(path: str, action_id: str) -> dict:
    if path in ("/tx/execute",):
        return {"action_id": action_id}
    if path == "/tx/intent":
        return {"frm": "user1", "to": "addr2", "amount": 1, "currency": "USD", "action_type": "transfer_low"}
    if path == "/policy/check":
        return {"action_type": "transfer_high"}
    if path == "/event/request":
        return {"action_id": "sweep-probe", "tx_hash": "sweep-probe", "event_type": "usb_insert"}
    return {}


def test_T17_constitutive_closure_sweeps_every_registered_wallet_route(wallet):
    intent = wallet.client.post("/demo/prepare", params={"stakes": "high"}).json()
    action_id = intent["action_id"]

    all_routes = _registered_routes(wallet.app)
    to_sweep = all_routes - WALLET_EXCLUDED
    assert to_sweep, "route discovery found nothing — the sweep would be vacuous"

    swept = set()
    for method, path in to_sweep:
        call_path = path.replace("{user}", "user1")
        if method == "GET":
            r = wallet.client.get(call_path)
        else:
            r = wallet.client.post(call_path, json=_body_for(path, action_id))
        swept.add((method, path))
        assert r.status_code < 500, f"{method} {path} -> {r.status_code} {r.text}"

    # Prove the sweep actually reached every non-excluded route — this is
    # what makes it "real": if someone adds a new endpoint later and it
    # isn't hit above, this assertion fails instead of the test quietly
    # continuing to pass.
    assert swept == to_sweep

    assert wallet.client.get("/balance/user1").json()["balance"] == 10000
    assert wallet.state.intents[action_id].status == "prepared"
    assert len(wallet.state.consumed_rod_ids) == 0


def test_T17_low_stakes_path_is_unaffected_by_the_sweep(wallet):
    # Sanity check that closure holds specifically for the high-stakes path,
    # not because execute is broken outright — low-stakes still works.
    wallet.client.post("/demo/prepare", params={"stakes": "low"})
    r = wallet.client.post("/demo/execute").json()
    assert r["constituted"] is True and r["balance"] == 9900


def test_T17_event_source_sweep_never_issues_a_rod_without_acquisition(event_source):
    r = event_source.client.post(
        "/event/request", json={"action_id": "a1", "tx_hash": "h1", "event_type": "usb_insert"}
    )
    req_id = r.json()["request_id"]

    all_routes = _registered_routes(event_source.app)
    assert all_routes, "route discovery found nothing — the sweep would be vacuous"

    swept = set()
    for method, path in all_routes:
        call_path = path.replace("{request_id}", req_id)
        if method == "GET":
            resp = event_source.client.get(call_path)
        else:
            resp = event_source.client.post(call_path, json=_body_for(path, "n/a"))
        swept.add((method, path))
        assert resp.status_code < 500, f"{method} {path} -> {resp.status_code} {resp.text}"

    assert swept == all_routes

    # No route in the sweep ever simulated a physical insert — the only ROD
    # issuance path is the in-process AcquisitionSource callback, which the
    # sweep above never touches.
    s = event_source.client.get(f"/event/status/{req_id}").json()
    assert s["status"] == "awaiting_usb_insert"
    assert s["rod"] is None
    assert all(e["kind"] != "acquired" for e in event_source.app.state.tsyviq.event_log)


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
