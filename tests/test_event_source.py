"""Event Source state machine. Mock-driven equivalents of T3-T8 and T16
(A.4 lists T5-T8/T16 as manual/hardware — these are the same assertions
exercised through MockUsbMonitor instead of a physical insert; the real
pyudev path is covered separately on the Ubuntu VM, see runbook/)."""

import time


def test_T3_event_request_never_issues_a_rod(event_source):
    r = event_source.client.post(
        "/event/request", json={"action_id": "a1", "tx_hash": "h1", "event_type": "usb_insert"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "awaiting_usb_insert"
    assert "rod" not in body

    s = event_source.client.get(f"/event/status/{body['request_id']}")
    assert s.json() == {"request_id": body["request_id"], "status": "awaiting_usb_insert", "rod": None}


def test_T4_no_public_usb_insert_style_endpoint_exists(event_source):
    for path in ("/event/usb_insert", "/event/confirm", "/event/acquire"):
        r = event_source.client.post(path, json={})
        assert r.status_code in (404, 405), path
        r = event_source.client.get(path)
        assert r.status_code in (404, 405), path


def test_T5_untrusted_usb_does_not_issue_rod(event_source):
    r = event_source.client.post(
        "/event/request", json={"action_id": "a1", "tx_hash": "h1", "event_type": "usb_insert"}
    )
    req_id = r.json()["request_id"]
    event_source.mock.simulate_insert(serial="someone-elses-drive")
    s = event_source.client.get(f"/event/status/{req_id}").json()
    assert s["status"] == "awaiting_usb_insert"
    assert s["rod"] is None


def test_T6_trusted_usb_with_one_pending_acquires_rod(event_source):
    r = event_source.client.post(
        "/event/request", json={"action_id": "a1", "tx_hash": "h1", "event_type": "usb_insert"}
    )
    req_id = r.json()["request_id"]
    event_source.mock.simulate_insert(label="TSYVIQ_DEMO_USB")
    s = event_source.client.get(f"/event/status/{req_id}").json()
    assert s["status"] == "acquired"
    rod = s["rod"]
    assert rod["action_id"] == "a1"
    assert rod["tx_hash"] == "h1"
    assert rod["request_id"] == req_id
    assert isinstance(rod["signature"], str) and len(rod["signature"]) > 0


def test_T7_trusted_usb_without_pending_request_issues_nothing(event_source):
    event_source.mock.simulate_insert(label="TSYVIQ_DEMO_USB")
    assert event_source.app.state.tsyviq.event_log[-1]["kind"] == "no_pending"
    assert event_source.app.state.tsyviq.requests_by_id == {}


def test_T8_multiple_pending_is_ambiguous_no_rod_for_either(event_source):
    r1 = event_source.client.post(
        "/event/request", json={"action_id": "a1", "tx_hash": "h1", "event_type": "usb_insert"}
    ).json()
    r2 = event_source.client.post(
        "/event/request", json={"action_id": "a2", "tx_hash": "h2", "event_type": "usb_insert"}
    ).json()
    event_source.mock.simulate_insert(label="TSYVIQ_DEMO_USB")

    s1 = event_source.client.get(f"/event/status/{r1['request_id']}").json()
    s2 = event_source.client.get(f"/event/status/{r2['request_id']}").json()
    assert s1["status"] == "awaiting_usb_insert" and s1["rod"] is None
    assert s2["status"] == "awaiting_usb_insert" and s2["rod"] is None
    assert event_source.app.state.tsyviq.event_log[-1]["kind"] == "ambiguous"


def test_T16_monitor_stopped_insert_is_dropped():
    from fastapi.testclient import TestClient

    from .conftest import MockUsbMonitor, event_mod

    mock = MockUsbMonitor()
    app = event_mod.create_app(monitor=mock)
    with TestClient(app) as client:
        client.post("/event/request", json={"action_id": "a1", "tx_hash": "h1", "event_type": "usb_insert"})
    # context exited -> shutdown -> monitor.stop()
    mock.simulate_insert(label="TSYVIQ_DEMO_USB")
    assert app.state.tsyviq.event_log == []


def test_duplicate_active_request_for_same_action_id_is_rejected(event_source):
    event_source.client.post("/event/request", json={"action_id": "a1", "tx_hash": "h1", "event_type": "usb_insert"})
    r = event_source.client.post(
        "/event/request", json={"action_id": "a1", "tx_hash": "h1-again", "event_type": "usb_insert"}
    )
    assert r.status_code == 409


def test_pending_request_expires_after_max_age_sec(event_source):
    r = event_source.client.post(
        "/event/request",
        json={"action_id": "a1", "tx_hash": "h1", "event_type": "usb_insert", "max_age_sec": 0},
    ).json()
    time.sleep(0.05)
    s = event_source.client.get(f"/event/status/{r['request_id']}").json()
    assert s["status"] == "expired"


def test_unsupported_event_type_rejected(event_source):
    r = event_source.client.post("/event/request", json={"action_id": "a1", "tx_hash": "h1", "event_type": "nfc_tap"})
    assert r.status_code == 400


def test_unknown_request_id_is_404(event_source):
    r = event_source.client.get("/event/status/does-not-exist")
    assert r.status_code == 404
