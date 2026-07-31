"""Create a throwaway payer wallet for a live x402 purchase.

Why a throwaway: paying with the key of a wallet that holds anything else puts
all of it in a plaintext file, for a purchase worth a few dollars. This address
holds exactly what is sent to it and is never reused, so the worst case is
losing that.

The private key is written straight to the file and never printed. Nobody
reading this program's output — a terminal, a log, a transcript, an assistant —
learns the key; only the address, which is public by nature.

    poetry run python scripts/new_payer_key.py .x402-mainnet.json
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

from eth_account import Account


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".x402-mainnet.json")
    if target.exists():
        # Overwriting is how a funded key gets lost with no way back.
        print(f"{target} already exists — refusing to overwrite it.")
        return 1
    if not target.name.startswith(".x402-") or target.suffix != ".json":
        print(f"name it .x402-<something>.json so .gitignore covers it; got {target.name}")
        return 1

    account = Account.create()
    target.write_text(
        json.dumps({"payer": {
            "private_key": account.key.hex(),
            "address": account.address,
        }}, indent=2),
        encoding="utf-8",
    )
    try:
        # Best effort: on Windows this narrows the file's ACL inheritance only
        # loosely, but on the Linux box it is the difference between 0600 and
        # world-readable.
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    print(f"wrote {target} (private key never displayed)")
    print()
    print(f"  address: {account.address}")
    print()
    print("Send USDC to that address ON BASE only. It is a fresh wallet: nothing")
    print("else is at risk, and it needs no ETH — the facilitator pays the gas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
