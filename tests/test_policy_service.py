from .conftest import policy_mod
from fastapi.testclient import TestClient

client = TestClient(policy_mod.app)


def test_transfer_high_requires_event():
    r = client.post("/policy/check", json={"action_type": "transfer_high"})
    assert r.status_code == 200
    assert r.json() == {
        "action_type": "transfer_high",
        "requires_event": True,
        "event_type": "usb_insert",
        "max_age_sec": 90,
    }


def test_transfer_low_does_not_require_event():
    r = client.post("/policy/check", json={"action_type": "transfer_low"})
    assert r.json()["requires_event"] is False


def test_unknown_action_type_is_404():
    r = client.post("/policy/check", json={"action_type": "nope"})
    assert r.status_code == 404


def test_policy_service_does_not_expose_balance_or_state_mutation():
    # Policy Service's entire route surface is request/response, read-only.
    routes = {getattr(r, "path", None) for r in policy_mod.app.routes}
    assert routes <= {"/policy/check", "/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
