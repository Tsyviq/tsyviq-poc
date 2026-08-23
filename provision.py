"""Generate the Event Source Ed25519 keypair.

event_sk.pem stays inside event-source/ forever — nothing else in this repo
reads it, and it is never served over any API. Wallet/Executor reads the
public key straight out of event-source/event_pk.pem (same machine, same
repo checkout) rather than receiving a copy, so there is exactly one file to
keep in sync when keys are rotated with --force.

Idempotent by default: refuses to overwrite an existing private key unless
--force is passed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from common import generate_keypair, private_key_to_pem, public_key_to_pem

EVENT_SOURCE_DIR = Path(__file__).parent / "event-source"
PRIVATE_KEY_PATH = EVENT_SOURCE_DIR / "event_sk.pem"
PUBLIC_KEY_PATH = EVENT_SOURCE_DIR / "event_pk.pem"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing keypair (invalidates any previously issued signatures).",
    )
    args = parser.parse_args()

    if PRIVATE_KEY_PATH.exists() and not args.force:
        print(
            f"refusing to overwrite existing {PRIVATE_KEY_PATH} (use --force to rotate)",
            file=sys.stderr,
        )
        sys.exit(1)

    EVENT_SOURCE_DIR.mkdir(parents=True, exist_ok=True)

    sk, pk = generate_keypair()

    PRIVATE_KEY_PATH.write_bytes(private_key_to_pem(sk))
    os.chmod(PRIVATE_KEY_PATH, 0o600)

    PUBLIC_KEY_PATH.write_bytes(public_key_to_pem(pk))
    os.chmod(PUBLIC_KEY_PATH, 0o644)

    print(f"private key: {PRIVATE_KEY_PATH} (0600, Event Source only)")
    print(f"public key:  {PUBLIC_KEY_PATH}")


if __name__ == "__main__":
    main()
