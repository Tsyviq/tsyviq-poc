"""Event Source (port 8003) — holds the private key, runs the USB Monitor,
issues ROD/T only after local acquisition of a trusted physical event.

Constitutive rules enforced here (CLAUDE-2.md §2, §11, §12, A.5):
- /event/request only ever creates a pending request. It NEVER issues a ROD.
- There is no public endpoint that accepts a digital claim of "USB inserted".
  The only path into on_device_event() is the in-process AcquisitionSource
  callback wired up at startup.
- action_id/tx_hash bound to a ROD come only from the server-side pending
  request created by /event/request — never from the acquisition callback,
  never from an HTTP caller.
- Exactly one active pending request may be unambiguously fulfilled at a
  time; if more than one is active when a trusted insert happens, no ROD is
  issued for any of them (§12, T8).

"consumed" (CLAUDE-2.md §11 diagram) is intentionally NOT tracked here. Once
a ROD is acquired, /event/status keeps returning it idempotently — that is
what lets both the Investor UI (polling for display) and the Wallet backend
(fetching it to verify) read the same acquired ROD without racing each
other over a one-shot read. Non-reuse of a ROD is enforced by the Executor's
own consumed_rods/event_instance_id bookkeeping (§16 point 8), which is the
side that actually matters for replay protection (T13) — Event Source
tracking its own "consumed" flag would be redundant bookkeeping that no
test in §18 depends on.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (  # noqa: E402
    age_seconds,
    canonical_json,
    load_private_key_pem,
    new_uuid,
    now_utc_iso,
    sha256_hex,
    sign_object,
)
from usb_monitor import AcquisitionSource, DeviceAttrs, default_monitor  # noqa: E402

EVENT_SOURCE_DIR = Path(__file__).resolve().parent
PRIVATE_KEY_PATH = EVENT_SOURCE_DIR / "event_sk.pem"
PUBLIC_KEY_PATH = EVENT_SOURCE_DIR / "event_pk.pem"
TRUSTED_USB_PATH = Path(os.environ.get("TRUSTED_USB_PATH", EVENT_SOURCE_DIR / "trusted_usb.json"))

SUPPORTED_EVENT_TYPES = {"usb_insert"}

AMBIGUOUS = object()  # sentinel: more than one active pending request


class EventRequestIn(BaseModel):
    action_id: str
    tx_hash: str
    event_type: str
    max_age_sec: int = 90


class EventRequestOut(BaseModel):
    request_id: str
    status: str


class EventStatusOut(BaseModel):
    request_id: str
    status: str
    rod: Optional[dict] = None


@dataclass
class PendingRequest:
    request_id: str
    action_id: str
    tx_hash: str
    event_type: str
    max_age_sec: int
    created_at: str
    status: str = "awaiting_usb_insert"
    rod: Optional[dict] = None


class EventSourceState:
    """All Event Source mutable state lives here so tests can construct
    isolated instances instead of sharing module-level globals."""

    def __init__(self, monitor: AcquisitionSource):
        self.monitor = monitor
        self.requests_by_id: dict[str, PendingRequest] = {}
        self.event_log: list[dict] = []

    def effective_status(self, req: PendingRequest) -> str:
        if req.status == "awaiting_usb_insert" and age_seconds(req.created_at) > req.max_age_sec:
            req.status = "expired"
        return req.status

    def active_pending_requests(self) -> list[PendingRequest]:
        return [r for r in self.requests_by_id.values() if self.effective_status(r) == "awaiting_usb_insert"]

    def get_current_pending(self):
        actives = self.active_pending_requests()
        if len(actives) == 0:
            return None
        if len(actives) == 1:
            return actives[0]
        return AMBIGUOUS

    def record(self, kind: str, attrs: DeviceAttrs, request_id: Optional[str] = None) -> None:
        self.event_log.append(
            {
                "kind": kind,
                "request_id": request_id,
                "serial": attrs.get("serial"),
                "label": attrs.get("label"),
                "at": now_utc_iso(),
            }
        )


def load_trusted_devices() -> list[dict]:
    import json

    return json.loads(TRUSTED_USB_PATH.read_text())["trusted_devices"]


def is_demo_trusted_usb(attrs: DeviceAttrs) -> bool:
    serial = attrs.get("serial")
    label = attrs.get("label")
    for dev in load_trusted_devices():
        t_serial, t_label = dev.get("serial"), dev.get("label")
        if t_serial and serial and t_serial == serial:
            return True
        if t_label and label and t_label == label:
            return True
    return False


def digest_local_usb_observable(attrs: DeviceAttrs) -> str:
    payload = {"serial": attrs.get("serial"), "label": attrs.get("label")}
    return sha256_hex(canonical_json(payload))


def compute_origin_commitment(
    local_observable_digest: str, request_id: str, tx_hash: str, event_instance_id: str, acquired_at: str
) -> str:
    payload = {
        "local_observable_digest": local_observable_digest,
        "request_id": request_id,
        "tx_hash": tx_hash,
        "event_instance_id": event_instance_id,
        "acquired_at": acquired_at,
    }
    return sha256_hex(canonical_json(payload))


def issue_rod(pending: PendingRequest, attrs: DeviceAttrs, sk) -> dict:
    event_instance_id = new_uuid()
    acquired_at = now_utc_iso()
    local_digest = digest_local_usb_observable(attrs)
    origin_commitment = compute_origin_commitment(
        local_digest, pending.request_id, pending.tx_hash, event_instance_id, acquired_at
    )
    device_id = f"demo:serial:{attrs.get('serial') or attrs.get('label') or 'unknown'}"
    rod = {
        "rod_id": new_uuid(),
        "request_id": pending.request_id,
        "action_id": pending.action_id,
        "tx_hash": pending.tx_hash,
        "event_type": pending.event_type,
        "event_instance_id": event_instance_id,
        "acquired_at": acquired_at,
        "device_id": device_id,
        "origin_commitment": origin_commitment,
    }
    rod["signature"] = sign_object(sk, rod)
    return rod


def create_app(monitor: Optional[AcquisitionSource] = None) -> FastAPI:
    if not PRIVATE_KEY_PATH.exists():
        raise RuntimeError(
            f"{PRIVATE_KEY_PATH} not found — run `python provision.py` from the repo root first"
        )
    event_sk = load_private_key_pem(PRIVATE_KEY_PATH)

    state = EventSourceState(monitor or default_monitor())

    def on_device_event(attrs: DeviceAttrs) -> None:
        pending = state.get_current_pending()
        if pending is None:
            state.record("no_pending", attrs)
            return
        if pending is AMBIGUOUS:
            state.record("ambiguous", attrs)
            return
        if not is_demo_trusted_usb(attrs):
            state.record("untrusted", attrs, request_id=pending.request_id)
            return
        rod = issue_rod(pending, attrs, event_sk)
        pending.status = "acquired"
        pending.rod = rod
        state.record("acquired", attrs, request_id=pending.request_id)

    app = FastAPI(title="TSYVIQ PoC — Event Source")
    app.state.tsyviq = state

    @app.on_event("startup")
    def _startup() -> None:
        state.monitor.start(on_device_event)

    @app.on_event("shutdown")
    def _shutdown() -> None:
        state.monitor.stop()

    @app.post("/event/request", response_model=EventRequestOut)
    def event_request(body: EventRequestIn) -> EventRequestOut:
        if body.event_type not in SUPPORTED_EVENT_TYPES:
            raise HTTPException(400, f"unsupported event_type: {body.event_type}")
        for r in state.requests_by_id.values():
            if r.action_id == body.action_id and state.effective_status(r) == "awaiting_usb_insert":
                raise HTTPException(
                    409, f"an active request already exists for this action_id: {r.request_id}"
                )
        req_id = new_uuid()
        state.requests_by_id[req_id] = PendingRequest(
            request_id=req_id,
            action_id=body.action_id,
            tx_hash=body.tx_hash,
            event_type=body.event_type,
            max_age_sec=body.max_age_sec,
            created_at=now_utc_iso(),
        )
        return EventRequestOut(request_id=req_id, status="awaiting_usb_insert")

    @app.get("/event/status/{request_id}", response_model=EventStatusOut)
    def event_status(request_id: str) -> EventStatusOut:
        pr = state.requests_by_id.get(request_id)
        if pr is None:
            raise HTTPException(404, "unknown request_id")
        status = state.effective_status(pr)
        return EventStatusOut(request_id=request_id, status=status, rod=pr.rod if status == "acquired" else None)

    @app.get("/event/public-key")
    def event_public_key() -> dict:
        return {"public_key_pem": PUBLIC_KEY_PATH.read_text()}

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
