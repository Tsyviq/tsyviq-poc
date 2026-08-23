"""Wallet / Executor (port 8001) — the only guarded commit path.

This is where CLAUDE-2.md §16's verification order and A.5's atomicity rule
live. Two things are structurally impossible by construction here, not just
checked at runtime:

- `/tx/execute` takes only `action_id`. There is no request field anywhere
  a caller can put a ROD — the ROD is always fetched server-side from Event
  Source via the intent's own `pending_request_id`. So "submit a forged
  ROD over HTTP" is not a reachable attack surface at all; T2 (fake
  ROD / invalid signature) is exercised directly against `verify_rod()` in
  tests, and `/demo/forge-attempt` (see below) demonstrates the same
  rejection for the Investor UI without opening any real bypass.
- `origin_commitment` is checked for format + signature coverage only,
  never recomputed — Wallet has no `local_observable_digest` and isn't
  supposed to (A.5).

`/demo/forge-attempt` is a demo-only illustration endpoint (not in
CLAUDE-2.md's §15 table, added for §17 Step 3's "forge fake ROD" UI beat).
It fabricates its own broken ROD server-side, runs it through the exact
same `verify_rod()` used by the real path, and returns the rejection — it
never accepts caller-supplied ROD content and never calls `recognize_cq`/
commit, so it cannot become a bypass. T17's endpoint sweep includes it.
"""

from __future__ import annotations

import base64
import fcntl
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (  # noqa: E402
    age_seconds,
    compute_tx_hash,
    load_public_key_pem,
    new_uuid,
    now_utc_iso,
    verify_object,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EVENT_SOURCE_PUBLIC_KEY_PATH = REPO_ROOT / "event-source" / "event_pk.pem"
LOCK_FILE_PATH = Path(__file__).resolve().parent / ".wallet.lock"
INDEX_HTML_PATH = Path(__file__).resolve().parent / "index.html"

POLICY_SERVICE_URL = os.environ.get("POLICY_SERVICE_URL", "http://127.0.0.1:8002")
EVENT_SOURCE_URL = os.environ.get("EVENT_SOURCE_URL", "http://127.0.0.1:8003")

DEMO_USER = "user1"
DEMO_RECIPIENT = "addr2"
DEMO_CURRENCY = "USD"
DEMO_START_BALANCE = 10000
DEMO_HIGH_AMOUNT = 6000
DEMO_LOW_AMOUNT = 100
HIGH_ACTION_TYPE = "transfer_high"
LOW_ACTION_TYPE = "transfer_low"


# ---------------------------------------------------------------------------
# Single-process guard (A.5 / Q2): flock is per open-file-description, so a
# second OS process (e.g. a stray `--workers 2`) genuinely fails to acquire
# it, unlike an in-memory flag. Tests that construct multiple isolated app
# instances in one process pass enable_process_guard=False.
# ---------------------------------------------------------------------------


def acquire_single_process_guard() -> None:
    fd = os.open(LOCK_FILE_PATH, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        raise RuntimeError(
            "Another Wallet/Executor process already holds the atomicity lock. "
            "This service must run as a single process (no --workers, no --reload) — "
            "see A.5/§16 point 11: the in-process lock only protects one process."
        ) from e


# ---------------------------------------------------------------------------
# Intent model
# ---------------------------------------------------------------------------


@dataclass
class Intent:
    action_id: str
    frm: str
    to: str
    amount: int
    currency: str
    action_type: str
    tx_hash: str
    status: str = "prepared"  # "prepared" | "executed"
    pending_request_id: Optional[str] = None


class WalletState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.balances: dict[str, int] = {DEMO_USER: DEMO_START_BALANCE}
        self.intents: dict[str, Intent] = {}
        self.consumed_rod_ids: set[str] = set()
        self.consumed_event_instance_ids: set[str] = set()
        self.demo_action_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Thin HTTP clients — injectable so tests can pass a TestClient(app) for
# Policy Service / Event Source instead of a real network connection.
# ---------------------------------------------------------------------------


class PolicyClient:
    def __init__(self, http_client) -> None:
        self.http = http_client

    def check(self, action_type: str) -> dict:
        r = self.http.post("/policy/check", json={"action_type": action_type})
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"policy service error: {r.text}")
        return r.json()


class EventSourceClient:
    def __init__(self, http_client) -> None:
        self.http = http_client

    def request_event(self, action_id: str, tx_hash: str, event_type: str, max_age_sec: int) -> dict:
        r = self.http.post(
            "/event/request",
            json={"action_id": action_id, "tx_hash": tx_hash, "event_type": event_type, "max_age_sec": max_age_sec},
        )
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"event source error: {r.text}")
        return r.json()

    def get_status(self, request_id: str) -> dict:
        r = self.http.get(f"/event/status/{request_id}")
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"event source error: {r.text}")
        return r.json()


# ---------------------------------------------------------------------------
# Executor verification — CLAUDE-2.md §16, checks run in the documented order
# ---------------------------------------------------------------------------


def verify_rod(
    intent: Intent,
    policy: dict,
    rod: dict,
    event_pk,
    consumed_rod_ids: set[str],
    consumed_event_instance_ids: set[str],
) -> tuple[bool, str]:
    if not verify_object(event_pk, rod, rod.get("signature", "")):
        return False, "invalid ROD signature"
    if rod.get("action_id") != intent.action_id:
        return False, "ROD action_id does not match executing action"
    expected_tx_hash = compute_tx_hash(intent.frm, intent.to, intent.amount, intent.currency, intent.action_type)
    if rod.get("tx_hash") != expected_tx_hash:
        return False, "ROD tx_hash does not match recomputed essential parameters"
    if rod.get("event_type") != policy.get("event_type"):
        return False, "ROD event_type does not match policy-required event_type"
    if rod.get("request_id") != intent.pending_request_id:
        return False, "ROD request_id does not match this action's pending request"
    acquired_at = rod.get("acquired_at")
    max_age_sec = policy.get("max_age_sec")
    if acquired_at is None or max_age_sec is None or age_seconds(acquired_at) > max_age_sec:
        return False, "ROD is expired (stale acquired_at)"
    origin_commitment = rod.get("origin_commitment")
    if not isinstance(origin_commitment, str) or len(origin_commitment) != 64:
        return False, "origin_commitment has invalid format"
    if rod.get("rod_id") in consumed_rod_ids:
        return False, "ROD already consumed (rod_id replay)"
    if rod.get("event_instance_id") in consumed_event_instance_ids:
        return False, "ROD already consumed (event_instance_id replay)"
    return True, "ok"


def recognize_cq(intent: Intent, rod: dict) -> dict:
    return {
        "action_id": intent.action_id,
        "tx_hash": intent.tx_hash,
        "rod_id": rod["rod_id"],
        "policy_ref": intent.action_type,
    }


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class IntentIn(BaseModel):
    frm: str
    to: str
    amount: int
    currency: str
    action_type: str


class IntentOut(BaseModel):
    action_id: str
    frm: str
    to: str
    amount: int
    currency: str
    action_type: str
    tx_hash: str
    status: str


class ExecuteIn(BaseModel):
    action_id: str


class ExecuteOut(BaseModel):
    action_id: str
    constituted: bool
    status: str  # "EXECUTED" | "NOT_CONSTITUTED"
    reason: Optional[str] = None
    balance: Optional[int] = None
    cq: Optional[dict] = None


class PolicyCheckIn(BaseModel):
    action_type: str


def intent_to_out(intent: Intent) -> IntentOut:
    return IntentOut(
        action_id=intent.action_id,
        frm=intent.frm,
        to=intent.to,
        amount=intent.amount,
        currency=intent.currency,
        action_type=intent.action_type,
        tx_hash=intent.tx_hash,
        status=intent.status,
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    policy_client: Optional[PolicyClient] = None,
    event_source_client: Optional[EventSourceClient] = None,
    enable_process_guard: bool = True,
) -> FastAPI:
    if enable_process_guard:
        acquire_single_process_guard()

    if not EVENT_SOURCE_PUBLIC_KEY_PATH.exists():
        raise RuntimeError(
            f"{EVENT_SOURCE_PUBLIC_KEY_PATH} not found — run `python provision.py` from the repo root first"
        )
    event_pk = load_public_key_pem(EVENT_SOURCE_PUBLIC_KEY_PATH)

    state = WalletState()
    policy = policy_client or PolicyClient(httpx.Client(base_url=POLICY_SERVICE_URL))
    events = event_source_client or EventSourceClient(httpx.Client(base_url=EVENT_SOURCE_URL))

    def execute_action(action_id: str) -> ExecuteOut:
        intent = state.intents.get(action_id)
        if intent is None:
            raise HTTPException(404, "unknown action_id")

        if intent.status == "executed":
            return ExecuteOut(
                action_id=action_id,
                constituted=True,
                status="EXECUTED",
                reason="already executed — replay is a no-op, balance unchanged",
                balance=state.balances.get(intent.frm),
            )

        pol = policy.check(intent.action_type)

        if not pol["requires_event"]:
            with state.lock:
                if intent.status == "executed":
                    return ExecuteOut(
                        action_id=action_id, constituted=True, status="EXECUTED", balance=state.balances.get(intent.frm)
                    )
                state.balances[intent.frm] = state.balances.get(intent.frm, 0) - intent.amount
                intent.status = "executed"
            return ExecuteOut(action_id=action_id, constituted=True, status="EXECUTED", balance=state.balances.get(intent.frm))

        if intent.pending_request_id is None:
            return ExecuteOut(
                action_id=action_id, constituted=False, status="NOT_CONSTITUTED", reason="no pending event request for this action"
            )

        ev = events.get_status(intent.pending_request_id)
        if ev.get("status") != "acquired" or ev.get("rod") is None:
            return ExecuteOut(
                action_id=action_id,
                constituted=False,
                status="NOT_CONSTITUTED",
                reason=f"ROD not acquired yet (event status: {ev.get('status')})",
            )
        rod = ev["rod"]

        # verify_rod (including its replay/consumed checks) and the commit
        # itself must be one atomic unit: checking replay status outside the
        # lock and committing inside it leaves a window where a second
        # concurrent request reads "already consumed" from a sibling
        # request's not-yet-finished commit and would incorrectly report
        # NOT_CONSTITUTED for an action that in fact did execute (T14).
        with state.lock:
            if intent.status == "executed":
                return ExecuteOut(
                    action_id=action_id, constituted=True, status="EXECUTED", balance=state.balances.get(intent.frm)
                )

            ok, reason = verify_rod(intent, pol, rod, event_pk, state.consumed_rod_ids, state.consumed_event_instance_ids)
            if not ok:
                return ExecuteOut(action_id=action_id, constituted=False, status="NOT_CONSTITUTED", reason=reason)

            cq = recognize_cq(intent, rod)
            state.consumed_rod_ids.add(rod["rod_id"])
            state.consumed_event_instance_ids.add(rod["event_instance_id"])
            state.balances[intent.frm] = state.balances.get(intent.frm, 0) - intent.amount
            intent.status = "executed"

        return ExecuteOut(
            action_id=action_id, constituted=True, status="EXECUTED", balance=state.balances.get(intent.frm), cq=cq
        )

    app = FastAPI(title="TSYVIQ PoC — Wallet / Executor")
    app.state.tsyviq = state

    @app.post("/tx/intent", response_model=IntentOut)
    def create_intent(body: IntentIn) -> IntentOut:
        action_id = new_uuid()
        tx_hash = compute_tx_hash(body.frm, body.to, body.amount, body.currency, body.action_type)
        intent = Intent(
            action_id=action_id,
            frm=body.frm,
            to=body.to,
            amount=body.amount,
            currency=body.currency,
            action_type=body.action_type,
            tx_hash=tx_hash,
        )
        state.intents[action_id] = intent
        return intent_to_out(intent)

    @app.post("/tx/execute", response_model=ExecuteOut)
    def tx_execute(body: ExecuteIn) -> ExecuteOut:
        return execute_action(body.action_id)

    @app.get("/balance/{user}")
    def get_balance(user: str) -> dict:
        return {"user": user, "balance": state.balances.get(user, 0)}

    @app.post("/policy/check")
    def policy_check(body: PolicyCheckIn) -> dict:
        return policy.check(body.action_type)

    @app.get("/demo/state")
    def demo_state() -> dict:
        if state.demo_action_id is None:
            return {"action_id": None}
        intent = state.intents[state.demo_action_id]
        event_status = events.get_status(intent.pending_request_id) if intent.pending_request_id else None
        return {
            "action_id": intent.action_id,
            "frm": intent.frm,
            "to": intent.to,
            "amount": intent.amount,
            "currency": intent.currency,
            "action_type": intent.action_type,
            "tx_hash": intent.tx_hash,
            "status": intent.status,
            "pending_request_id": intent.pending_request_id,
            "event_status": event_status,
            "balance": state.balances.get(intent.frm),
        }

    @app.post("/demo/prepare", response_model=IntentOut)
    def demo_prepare(stakes: str = "high") -> IntentOut:
        if stakes == "high":
            amount, action_type = DEMO_HIGH_AMOUNT, HIGH_ACTION_TYPE
        elif stakes == "low":
            amount, action_type = DEMO_LOW_AMOUNT, LOW_ACTION_TYPE
        else:
            raise HTTPException(400, "stakes must be 'high' or 'low'")
        action_id = new_uuid()
        tx_hash = compute_tx_hash(DEMO_USER, DEMO_RECIPIENT, amount, DEMO_CURRENCY, action_type)
        intent = Intent(
            action_id=action_id,
            frm=DEMO_USER,
            to=DEMO_RECIPIENT,
            amount=amount,
            currency=DEMO_CURRENCY,
            action_type=action_type,
            tx_hash=tx_hash,
        )
        state.intents[action_id] = intent
        state.demo_action_id = action_id
        return intent_to_out(intent)

    @app.post("/demo/execute-no-rod", response_model=ExecuteOut)
    def demo_execute_no_rod() -> ExecuteOut:
        if state.demo_action_id is None:
            raise HTTPException(400, "call /demo/prepare first")
        return execute_action(state.demo_action_id)

    @app.post("/demo/request-event")
    def demo_request_event() -> dict:
        if state.demo_action_id is None:
            raise HTTPException(400, "call /demo/prepare first")
        intent = state.intents[state.demo_action_id]
        pol = policy.check(intent.action_type)
        if not pol["requires_event"]:
            raise HTTPException(400, "current demo action does not require an event")
        resp = events.request_event(intent.action_id, intent.tx_hash, pol["event_type"], pol["max_age_sec"])
        intent.pending_request_id = resp["request_id"]
        return resp

    @app.post("/demo/execute", response_model=ExecuteOut)
    def demo_execute() -> ExecuteOut:
        if state.demo_action_id is None:
            raise HTTPException(400, "call /demo/prepare first")
        return execute_action(state.demo_action_id)

    @app.post("/demo/reset")
    def demo_reset() -> dict:
        with state.lock:
            state.balances = {DEMO_USER: DEMO_START_BALANCE}
            state.intents.clear()
            state.consumed_rod_ids.clear()
            state.consumed_event_instance_ids.clear()
            state.demo_action_id = None
        return {"status": "reset", "balance": DEMO_START_BALANCE}

    @app.post("/demo/forge-attempt")
    def demo_forge_attempt() -> dict:
        """Illustration only (§17 Step 3). Fabricates its own broken ROD,
        runs it through the real verify_rod(), never accepts caller input,
        never calls recognize_cq or commits. See module docstring."""
        if state.demo_action_id is None:
            raise HTTPException(400, "call /demo/prepare first")
        intent = state.intents[state.demo_action_id]
        pol = policy.check(intent.action_type)
        fake_rod = {
            "rod_id": new_uuid(),
            "request_id": intent.pending_request_id or new_uuid(),
            "action_id": intent.action_id,
            "tx_hash": intent.tx_hash,
            "event_type": pol.get("event_type") or "usb_insert",
            "event_instance_id": new_uuid(),
            "acquired_at": now_utc_iso(),
            "device_id": "demo:serial:FORGED",
            "origin_commitment": "0" * 64,
            "signature": base64.b64encode(b"not-a-real-ed25519-signature").decode("ascii"),
        }
        ok, reason = verify_rod(intent, pol, fake_rod, event_pk, state.consumed_rod_ids, state.consumed_event_instance_ids)
        return {"cq_formed": ok, "reason": reason, "constituted": False}

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def investor_ui() -> str:
        return INDEX_HTML_PATH.read_text()

    return app


app = create_app()
