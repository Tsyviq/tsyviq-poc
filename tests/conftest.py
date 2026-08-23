"""Shared fixtures for the automated suite (A.4: no physical USB needed).

All three services run in-process via FastAPI TestClient — Wallet's
PolicyClient/EventSourceClient are pointed at TestClient instances instead
of real httpx connections, so the tests exercise the real request/response
contracts between services without needing three running processes or
three ports. USB insertion is simulated through MockUsbMonitor, which
drives the exact same in-process callback a real pyudev event would.

The manual/hardware tests (T5-T8, T15, T16 per A.4, plus §19) are covered
for real physical insertion separately in runbook/VM_RUNBOOK.md against the
real PyudevUsbMonitor on the Ubuntu ARM64 VM — this file only ever imports
MockUsbMonitor.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "event-source"))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


policy_mod = _load_module("policy_main", REPO / "policy-service" / "main.py")
event_mod = _load_module("event_main", REPO / "event-source" / "main.py")
wallet_mod = _load_module("wallet_main", REPO / "wallet-simulator" / "main.py")

import common  # noqa: E402
from usb_monitor import MockUsbMonitor  # noqa: E402


def build_signed_rod(event_sk, **fields) -> dict:
    """A fully, validly-signed ROD with caller-chosen field values.

    Used only to unit-test verify_rod()'s individual assertions in
    isolation (T9-T12) — see tests/test_wallet_executor.py for why the real
    HTTP flow structurally cannot produce a mismatched ROD (Event Source
    always derives action_id/tx_hash/event_type from its own pending
    request, never from a caller).
    """
    base = {
        "rod_id": common.new_uuid(),
        "request_id": "req-x",
        "action_id": "act-x",
        "tx_hash": "hash-x",
        "event_type": "usb_insert",
        "event_instance_id": common.new_uuid(),
        "acquired_at": common.now_utc_iso(),
        "device_id": "demo:serial:TEST",
        "origin_commitment": "a" * 64,
    }
    base.update(fields)
    base["signature"] = common.sign_object(event_sk, base)
    return base


@pytest.fixture()
def event_sk():
    return common.load_private_key_pem(REPO / "event-source" / "event_sk.pem")


@pytest.fixture()
def event_pk():
    return common.load_public_key_pem(REPO / "event-source" / "event_pk.pem")


@pytest.fixture()
def policy_client():
    return wallet_mod.PolicyClient(TestClient(policy_mod.app))


@pytest.fixture()
def event_source():
    """(mock_monitor, TestClient, app) with lifespan (monitor.start) active."""
    mock = MockUsbMonitor()
    app = event_mod.create_app(monitor=mock)
    with TestClient(app) as client:
        yield SimpleNamespace(mock=mock, client=client, app=app)


@pytest.fixture()
def wallet(policy_client, event_source):
    event_client = wallet_mod.EventSourceClient(event_source.client)
    app = wallet_mod.create_app(
        policy_client=policy_client, event_source_client=event_client, enable_process_guard=False
    )
    client = TestClient(app)
    return SimpleNamespace(client=client, app=app, state=app.state.tsyviq, mock=event_source.mock, event_source=event_source)


def prepare_and_acquire_high(w) -> dict:
    """Full happy path up to (but not including) execute: prepare 6000,
    request event, insert trusted USB. Returns the /demo/prepare intent."""
    intent = w.client.post("/demo/prepare", params={"stakes": "high"}).json()
    w.client.post("/demo/request-event")
    w.mock.simulate_insert(label="TSYVIQ_DEMO_USB")
    return intent
