# TSYVIQ PoC — USB Insert

A proof of concept for **causal separation of the digital and material control contours**: a digital agent can prepare any number of valid requests, but a high-stakes action does not execute until a real physical event (a trusted USB insertion) has occurred and been locally acquired by Event Source.

> **Central claim, in one sentence:** an agent can create as much correctly-formed digital data (BOD) as it wants and call any public API, but it cannot cause Event Source to acquire a qualifying material-origin trace (ROD/T), or cause Executor to form a current Cq, by digital request alone. Until that trace exists, the protected transfer stays **NOT CONSTITUTED**, and the single guarded commit path stays closed.

## Architecture

Three services, one causal boundary:

```
Agent/Bot --intent--> Policy Service (8002)  [BOD: requires_event?]
   |
   +--POST /event/request--> Event Source (8003)  [awaiting_usb_insert, no ROD yet]
   |
=========================== CAUSAL BOUNDARY ============================
   trusted USB physically inserted
=========================== ACQUISITION BOUNDARY ========================
   |
   USB Monitor (in-process, pyudev on Linux) --callback--> Event Source
   forms ROD/T (signed, bound to action_id/tx_hash/request_id)
   |
   v
Wallet/Executor (8001) verifies ROD/T -> forms/recognizes Cq -> atomic commit
```

- `/event/request` **never** issues a ROD — it only creates a pending request.
- There is **no public endpoint** that lets a digital caller claim "USB inserted" (`/event/usb_insert`, `/event/confirm`, `/event/acquire` do not exist — verified both by the automated test suite and live against running servers).
- `/tx/execute` takes only `action_id`. There is no field anywhere a caller can put a ROD; Executor always fetches it itself from Event Source.
- Executor independently recomputes `tx_hash`, verifies signature/binding/freshness/non-reuse, and only *then* forms/recognizes a current **Cq**. Only Cq — never a raw ROD — opens the single guarded commit path.

## Terminology

| Term | Meaning |
|---|---|
| **BOD** — Bit-Origin Data | Anything a digital agent can produce by itself: an intent, a policy result, a pending-request status. Never sufficient on its own. |
| **ROD** — Reality-Origin Data | A digital trace whose valid appearance depends on a real physical event, acquired locally by Event Source. Not defined by format (it's JSON, like everything else) — defined by causal origin. |
| **Cq** | The internal Executor-recognized constitutive component. Formed/recognized only after full verification of a qualifying ROD/T. Raw/verified ROD is *not itself* an authorization token — only Cq is. |

Patent mapping used throughout the code/UI: **ROD/T → current Cq → constituted state**.

## Quick start (macOS, dev/mock mode)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if not already installed
export PATH="$HOME/.local/bin:$PATH"

uv venv --python 3.12
uv pip install -r requirements.txt

python provision.py            # generates event-source/event_sk.pem + event_pk.pem

uv run python run_tests.py     # 39 automated tests, no physical USB needed
```

Run the three services (each in its own terminal, from the repo root):

```bash
PYTHONPATH="$PWD" uv run uvicorn main:app --app-dir policy-service --port 8002
PYTHONPATH="$PWD" uv run uvicorn main:app --app-dir event-source   --port 8003
PYTHONPATH="$PWD" uv run uvicorn main:app --app-dir wallet-simulator --port 8001
```

Open **http://127.0.0.1:8001/** for the Investor UI.

On macOS there is no real `pyudev`, so Event Source runs with `MockUsbMonitor` — there is deliberately **no way to simulate an insert from outside the process** (that's the whole point). The "insert the trusted USB" step of the live demo only works on real Linux with real `pyudev` — see `runbook/VM_RUNBOOK.md` for the Ubuntu ARM64/UTM setup used for that.

## Demo script (matches the Investor UI buttons)

1. **Prepare 6000** — creates a `transfer_high` intent, `user1 → addr2`. Shows `action_id`/`tx_hash` (BOD).
2. **Execute without Cq** — `NOT CONSTITUTED / NO COMMIT`, balance unchanged.
3. **Forge fake ROD/T** — a fabricated ROD is run through the real `verify_rod()` and rejected (`Cq NOT FORMED`, invalid signature).
4. **Request Event** — `awaiting_usb_insert`. Still no ROD.
5. Insert the trusted demo USB (Linux/VM only). UI polls `/demo/state` every ~900ms.
6. ROD/T acquired — UI shows `rod_id`, `device_id`, `origin_commitment`, `acquired_at`.
7. **Execute** — `Cq FORMED → CONSTITUTED → EXECUTED`. Balance **10000 → 4000**, shown large with a flash.
8. **Replay Execute** — no second Cq, no second commit, balance stays **4000**.

A **Low-stakes 100** button demonstrates the same wallet executing without any ROD at all (policy-driven). **Reset Demo** restores the 10000 balance and clears all intents/pending requests for the next run-through.

## Test coverage (T1–T18, per the spec's numbering)

`run_tests.py` / `pytest tests/` — **39 automated tests**, no physical hardware, all three services wired together in-process via FastAPI `TestClient`, USB insertion simulated through `MockUsbMonitor` (the exact same in-process callback path a real `pyudev` event drives).

- **T1, T2, T3, T4, T9–T14, T17, T18** — the officially-automated set (no-ROD execute, forged signature, request/status contract, no fake-insert endpoint, field-mismatch rejection, replay, concurrent double-execute, constitutive-closure sweep, fake-field smuggling).
- **T5–T8, T15, T16** — spec lists these as manual/hardware (real insertion needed). This repo *additionally* covers them automatically via `MockUsbMonitor` (untrusted device, trusted device, no pending request, ambiguous multi-pending, low-stakes without ROD, monitor-stopped) — extra coverage on top of, not instead of, the real-hardware run.
- T9–T12 specifically are unit tests of `verify_rod()` with a genuinely-signed-but-mismatched ROD, not HTTP round trips — see `tests/test_wallet_executor.py` docstring for why the real flow structurally cannot produce a mismatched ROD in the first place (Event Source always derives `action_id`/`tx_hash`/`event_type` from its own pending-request record, never from a caller).

The **real** `pyudev` path and the full §19 physical acceptance walkthrough are run by hand on the Ubuntu ARM64 UTM VM — see `runbook/VM_RUNBOOK.md`.

## Honest limitations (named next milestones, not "TODO later")

This PoC proves the **architectural** claim above under a specific trusted-boundary threat model. It does **not** claim to be production security. Specifically out of scope, by design, for this PoC:

- **Theft of `event_sk.pem`** (remote file read or local access to the Event Source host). *Next milestone:* hardware root of trust (HSM/TPM) where the key is unreadable even with root.
- **Compromise of the Event Source host's OS/kernel**, or hardware-level USB emulation/serial spoofing. *Next milestone:* physical separation of Event Source from the agent's compute, on different hardware.
- **Agent and Event Source co-located on the same machine.** In this PoC all three services run as three processes on one host (one OS user), which is exactly the boundary A.6 asks to name rather than paper over. *Next milestone:* process/container isolation with no filesystem or process access from the agent's sandbox to Event Source's.
- **USB serial/label** (`trusted_usb.json`) is a **demo identification convenience only** — explicitly not a production security credential and not a hardware root of trust.
- This PoC demonstrates one specific software/hardware-separated embodiment under the stated threat model; it does not claim to demonstrate the strongest possible (all-keys-present / key-neutral) embodiment of the underlying architecture.

## Repository layout

```
tsyviq-poc/
├── README.md
├── requirements.txt
├── provision.py            # Ed25519 keypair generation (idempotent, private key chmod 600)
├── run_tests.py            # pytest tests/ wrapper — automated suite only
├── common.py                # canonical JSON, sha256, Ed25519 sign/verify — shared by all 3 services
├── wallet-simulator/
│   ├── main.py               # Wallet/Executor (8001) + Investor UI route
│   └── index.html
├── policy-service/
│   ├── main.py                # Policy Service (8002)
│   └── policies.json
├── event-source/
│   ├── main.py                 # Event Source (8003): pending-request state machine
│   ├── usb_monitor.py            # AcquisitionSource interface: MockUsbMonitor / PyudevUsbMonitor
│   └── trusted_usb.json          # demo device identification (real value set on the VM)
├── tests/                     # automated suite (see above)
└── runbook/
    └── VM_RUNBOOK.md            # Ubuntu ARM64/UTM setup + real pyudev + manual T5-T8/T15/T16/§19
```

`event_sk.pem`/`event_pk.pem` are generated locally by `provision.py` in each environment and are `.gitignore`d — they never leave the machine they were generated on, including over git/GitHub.
