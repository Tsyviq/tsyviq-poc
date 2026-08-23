# VM Runbook — real pyudev acquisition on Ubuntu ARM64 / UTM

This is the only place the **real** material-origin acquisition path (real
`pyudev`, real physical USB insertion) gets exercised. Everything in
`tests/` runs on mocks and proves the logic; this runbook proves the
physical embodiment actually works on the target machine.

Target environment (already provisioned, per project decisions):
**Ubuntu 26.04 LTS ARM64 Desktop, UTM (QEMU engine), MacBook Air M5, 6GB RAM,
64GB virtual disk, hardware OpenGL disabled.**

Everything below is a command you run yourself inside the VM (or in UTM's
GUI where noted) — this machine session cannot reach into the VM.

## 0. Boot the VM

Open UTM.app on the Mac, select the Ubuntu VM, **Run**. Log in to the
Ubuntu Desktop session.

## 1. System packages

Open a terminal in the Ubuntu Desktop and install the build/runtime
dependencies `pyudev` and `cryptography` need on ARM64 (no prebuilt wheels
for `cryptography`+`pyudev` combo can be assumed on a fresh minimal image —
these give you a working compile toolchain and the actual udev headers):

```bash
sudo apt update
sudo apt install -y build-essential python3-dev pkg-config libssl-dev \
    libudev-dev udev curl git
```

## 2. Install uv + Python 3.12

Same tool as macOS — no system Python version dependency:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"   # or open a new terminal
uv python install 3.12
```

## 3. Get the code onto the VM

The repo is **private** on GitHub — the VM needs its own read access,
separate from the Mac. Pick one:

**Option A — gh CLI in the VM too:**
```bash
curl -sSL https://github.com/cli/cli/releases/latest/download/gh_linux_arm64.tar.gz \
  # (check the actual latest asset name/version at https://github.com/cli/cli/releases first)
gh auth login --web
gh repo clone <your-github-username>/<repo-name>
```

**Option B — SSH deploy key:**
```bash
ssh-keygen -t ed25519 -C "tsyviq-vm" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
# Add this key on GitHub: repo -> Settings -> Deploy keys -> Add deploy key
# (read-only is enough)
git clone git@github.com:<your-github-username>/<repo-name>.git
```

```bash
cd <repo-name>
uv venv --python 3.12
uv pip install -r requirements.txt
```

## 4. Provision Event Source's keypair — fresh, on this machine

**Do not copy `event_sk.pem`/`event_pk.pem` from the Mac.** Each
environment gets its own pair; the private key never leaves the machine it
was generated on (§8, A.6).

```bash
uv run python provision.py
```

## 5. Sanity check: the mock-based suite still passes here

The automated tests always construct `MockUsbMonitor` explicitly — they
never touch `pyudev` — so this is a fast, platform-independent sanity
check that the code itself is intact after the transfer, before touching
any hardware:

```bash
uv run python run_tests.py
```

Expect: `39 passed`.

## 6. Enable USB passthrough in UTM (GUI — this part only)

1. In UTM, **shut down** the VM if it's running (Settings changes to USB
   controller require a restart on most UTM/QEMU configurations).
2. Select the VM → **Edit** → **USB** → enable **USB Sharing** and set the
   controller to **USB 3.0 (XHCI)** (this VM already exists, so this may
   already be configured — check before assuming it needs changing).
3. Save, **boot** the VM again.
4. Plug the trusted demo USB drive into the **Mac's** physical USB port.
5. In the running VM's window, open the **USB** menu in UTM's toolbar/menu
   bar and select the drive to **redirect it into the guest**. (macOS will
   stop showing it as a mounted volume on the host once redirected — that's
   expected, it now belongs to the guest.)
6. Inside Ubuntu: `lsusb` should list the device. If it doesn't appear,
   the passthrough step above didn't take — recheck step 2/5 before going
   further.

## 7. Confirm udev actually sees insert events

This is the single most important check in A.3 — do this before trusting
anything else:

```bash
udevadm monitor --subsystem-match=block
```

Unplug and re-plug the redirected drive (via the UTM USB menu, detach then
re-attach) and confirm `UDEV [...] add` events appear for `subsystem=block`
with `DEVTYPE=disk`. If nothing prints here, `PyudevUsbMonitor` will never
fire either — stop and fix this first.

## 8. Identify the trusted device and fill in `trusted_usb.json`

```bash
lsblk -o NAME,SERIAL,LABEL,TRAN
udevadm info --query=property --name=/dev/sdX | grep -E 'ID_SERIAL_SHORT|ID_FS_LABEL'
```

(Replace `/dev/sdX` with whatever `lsblk` showed for the redirected drive —
**not** the VM's own virtual disk.)

Edit `event-source/trusted_usb.json`, replacing the placeholder serial with
the real `ID_SERIAL_SHORT` value:

```json
{
  "trusted_devices": [
    { "serial": "<real ID_SERIAL_SHORT value>", "label": "TSYVIQ_DEMO_USB" }
  ]
}
```

Match is serial-first, label-fallback — you only strictly need the serial
filled in correctly.

## 9. Run the three real services

```bash
cd <repo-name>
PYTHONPATH="$PWD" uv run uvicorn main:app --app-dir policy-service --port 8002 &
PYTHONPATH="$PWD" uv run uvicorn main:app --app-dir event-source   --port 8003 &
PYTHONPATH="$PWD" uv run uvicorn main:app --app-dir wallet-simulator --port 8001 &
```

On Linux, `usb_monitor.default_monitor()` automatically selects
`PyudevUsbMonitor` — no code or env var changes needed versus macOS.

Open Firefox **inside the Ubuntu Desktop** (not on the Mac — the VM has its
own screen) at `http://127.0.0.1:8001/`.

## 10. Manual/hardware tests (A.4: T5–T8, T15, T16)

The automated suite already covers the logic behind all of these via
`MockUsbMonitor`; this is about confirming the **real** `pyudev` path
produces the same behavior against real hardware.

| Test | Steps | Expected |
|---|---|---|
| **T5** | Prepare 6000 → Request Event. Redirect a **different**, untrusted USB drive into the VM. | UI stays `AWAITING MATERIAL EVENT`. No ROD in `/demo/state`. |
| **T6** | Same pending request. Redirect the **trusted** demo USB. | `/demo/state` shows `event_status.status == "acquired"` with a populated `rod`. |
| **T7** | Reset. Redirect the trusted USB **without** calling Request Event first. | Nothing happens — `event-source` log shows `no_pending`, no request exists to attach a ROD to. |
| **T8** | Prepare and Request Event for **two** different intents (e.g. call `POST /tx/intent` directly for a second one, then `POST /event/request` for it) so two pending requests are active. Insert the trusted USB. | Neither pending request acquires a ROD — check both via `/event/status/{request_id}`. |
| **T15** | Reset. Click **Low-stakes 100**. | Executes immediately, no USB involved at all. |
| **T16** | Stop the Event Source process (`kill` the `--port 8003` uvicorn, or send SIGTERM) *before* inserting. Insert the trusted USB (or just try — with no process listening, nothing can observe it anyway). Restart Event Source, confirm the earlier pending request is gone (fresh in-memory state) and high-stakes still requires a fresh Request Event + insert. | No ROD from the insert that happened while stopped; balance never moved. |

## 11. Full §19 acceptance walkthrough (do this last, it's the actual demo rehearsal)

1. Start all three services, open the UI.
2. **Prepare 6000**.
3. **Execute without Cq** → `NOT CONSTITUTED / NO COMMIT`.
4. **Request Event** → `awaiting_usb_insert`.
5. Confirm no ROD exists yet (`/demo/state`).
6. Redirect an **untrusted** USB drive → confirm no ROD appears.
7. Redirect the **trusted** demo USB → confirm ROD/T appears (`acquired`).
8. **Execute** → `Cq FORMED/RECOGNIZED`, balance drops once (10000 → 4000).
9. **Replay Execute** → no second Cq, balance stays at 4000.
10. **Low-stakes 100** → executes without touching USB at all.
11. Before ever redirecting the trusted USB in a fresh run, try every
    public/demo endpoint (`curl` is fine) and confirm none of them changes
    `user1`'s balance — this is T17 rehearsed by hand, on real hardware,
    right before an investor sees it.

If all eleven steps hold, the PoC is ready to present.
