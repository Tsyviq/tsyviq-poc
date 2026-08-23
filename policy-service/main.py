"""Policy Service (port 8002).

Pure digital rule lookup: given an action_type, says whether a qualifying
external event is required before the action can be constituted, and (if
so) which event_type and max_age_sec apply. This is BOD — it decides
nothing about whether any given action actually executes; it never touches
balance or consequential state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

POLICIES_PATH = Path(__file__).parent / "policies.json"

app = FastAPI(title="TSYVIQ PoC — Policy Service")


def load_policies() -> dict:
    return json.loads(POLICIES_PATH.read_text())


class PolicyCheckRequest(BaseModel):
    action_type: str


class PolicyResponse(BaseModel):
    action_type: str
    requires_event: bool
    event_type: Optional[str] = None
    max_age_sec: Optional[int] = None


@app.post("/policy/check", response_model=PolicyResponse)
def policy_check(req: PolicyCheckRequest) -> PolicyResponse:
    policies = load_policies()
    policy = policies.get(req.action_type)
    if policy is None:
        raise HTTPException(status_code=404, detail=f"unknown action_type: {req.action_type}")
    return PolicyResponse(
        action_type=req.action_type,
        requires_event=policy["requires_event"],
        event_type=policy.get("event_type"),
        max_age_sec=policy.get("max_age_sec"),
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
