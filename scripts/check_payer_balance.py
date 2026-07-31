"""Has the payer wallet actually received the USDC, and on the right chain?

Worth its own step because the failure it catches is quiet: USDC sent over the
wrong network arrives at the same address on a chain the service does not
accept, so the wallet "has the money" everywhere except where it counts. The
purchase would then fail on an insufficient balance that looks like a bug.

    poetry run python scripts/check_payer_balance.py .x402-mainnet.json --network base
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.verify_deployment import KNOWN_NETWORKS  # noqa: E402

# Public endpoints, no key required. Only ever used for eth_call.
RPC = {
    "base": "https://mainnet.base.org",
    "base-sepolia": "https://sepolia.base.org",
}
# balanceOf(address)
BALANCE_OF = "0x70a08231"


def balance(rpc: str, contract: str, address: str) -> int:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [
            {"to": contract, "data": BALANCE_OF + address[2:].lower().rjust(64, "0")},
            "latest",
        ],
    }
    result = httpx.post(rpc, json=payload, timeout=30).json()
    if "error" in result:
        raise SystemExit(f"RPC refused: {result['error']}")
    return int(result["result"], 16)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keys", type=Path, nargs="?", default=Path(".x402-mainnet.json"))
    parser.add_argument("--network", default="base", choices=sorted(RPC))
    parser.add_argument("--need", type=int, default=3_000_000,
                        help="atomic units the purchase requires (default 3 USDC)")
    args = parser.parse_args()

    if not args.keys.exists():
        print(f"{args.keys} does not exist; run scripts/new_payer_key.py first")
        return 1
    # Only the address is read out of the file. The key stays where it is.
    address = json.loads(args.keys.read_text(encoding="utf-8"))["payer"]["address"]
    contract = KNOWN_NETWORKS[args.network]["contract"]

    held = balance(RPC[args.network], contract, address)
    print(f"payer   {address}")
    print(f"network {args.network}")
    print(f"USDC    {held / 1e6:.2f}  (need {args.need / 1e6:.2f})")

    if held >= args.need:
        print("\nREADY — enough USDC on the right chain to buy a build.")
        return 0

    # Say plainly where else to look, since "wrong network" is the likely cause
    # and it presents as an empty wallet rather than as a mistake.
    print("\nNOT READY on this network.")
    for other in sorted(RPC):
        if other == args.network:
            continue
        elsewhere = balance(RPC[other], KNOWN_NETWORKS[other]["contract"], address)
        if elsewhere:
            print(f"  ...but {elsewhere / 1e6:.2f} USDC is sitting on {other}.")
    print("  If nothing shows anywhere, the transfer has not confirmed yet, or it")
    print("  went to a chain this script does not check.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
