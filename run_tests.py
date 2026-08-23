"""Automated suite entrypoint (A.4).

Everything under tests/ requires no physical USB and no running services —
all three FastAPI apps are exercised in-process via TestClient, and USB
insertion is simulated through MockUsbMonitor. This is what CI runs.

The manual/hardware tests (T5-T8, T15, T16 per A.4's split, plus the full
§19 acceptance walkthrough) live in runbook/VM_RUNBOOK.md and are run by
hand against the real PyudevUsbMonitor on the Ubuntu ARM64 VM — they are
NOT part of this script, on purpose: they need a physical trusted USB
device and cannot run headless in CI.

Usage: `uv run python run_tests.py` (or `pytest tests/ -v` directly).
"""

import subprocess
import sys

if __name__ == "__main__":
    result = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-v", *sys.argv[1:]])
    sys.exit(result.returncode)
