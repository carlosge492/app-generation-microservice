"""Who has paid, and when — read from the chain rather than from the service.

Deliberately not a service endpoint. The money arrives on-chain whether or not
the API is up, whether or not the build then succeeded, and whether or not the
job record survived a restart. A sale is a USDC Transfer into X402_PAY_TO, and
that is the one record no bug on this side can lose.

    poetry run python scripts/watch_sales.py                    # recent sales
    poetry run python scripts/watch_sales.py --network base-sepolia
    poetry run python scripts/watch_sales.py --follow           # keep watching
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.check_payer_balance import RPC  # noqa: E402
from scripts.verify_deployment import KNOWN_NETWORKS  # noqa: E402

# keccak256("Transfer(address,address,uint256)")
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# Base produces a block every 2 seconds, so a day is ~43200 blocks. The public
# RPCs cap a single eth_getLogs at 2000 blocks and reject anything wider, so
# every query is windowed rather than sized to the question being asked.
WINDOW = 2_000


def rpc(url: str, method: str, params: list) -> object:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    result = httpx.post(url, json=body, timeout=45).json()
    if "error" in result:
        raise SystemExit(f"RPC refused {method}: {result['error']}")
    return result["result"]


def topic_address(address: str) -> str:
    return "0x" + address[2:].lower().rjust(64, "0")


def sales(url: str, contract: str, pay_to: str, from_block: int, to_block: int) -> list[dict]:
    found: list[dict] = []
    start = from_block
    while start <= to_block:
        end = min(start + WINDOW - 1, to_block)
        logs = rpc(url, "eth_getLogs", [{
            "address": contract,
            "topics": [TRANSFER, None, topic_address(pay_to)],
            "fromBlock": hex(start), "toBlock": hex(end),
        }])
        for entry in logs:
            found.append({
                "block": int(entry["blockNumber"], 16),
                "tx": entry["transactionHash"],
                # topics[1] is the sender, right-aligned in 32 bytes.
                "payer": "0x" + entry["topics"][1][-40:],
                "usdc": int(entry["data"], 16) / 1e6,
            })
        start = end + 1
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", default="base", choices=sorted(RPC))
    parser.add_argument("--pay-to", default="0xb8F78c20053702B6F4141609C4d7a37bcaD82bC9")
    parser.add_argument("--blocks", type=int, default=43_200,
                        help="how far back to look; Base is ~43200 blocks a day")
    parser.add_argument("--follow", action="store_true",
                        help="keep polling for new sales until interrupted")
    parser.add_argument("--price", type=int, default=3_000_000,
                        help="atomic price of one build; transfers of any other "
                             "amount are reported as deposits, not sales")
    args = parser.parse_args()

    url = RPC[args.network]
    contract = KNOWN_NETWORKS[args.network]["contract"]
    head = int(rpc(url, "eth_blockNumber", []), 16)
    start = max(0, head - args.blocks)

    print(f"watching {args.pay_to} for USDC on {args.network}")
    found = sales(url, contract, args.pay_to, start, head)

    # Any incoming transfer looks identical on-chain, so the price is what
    # separates a purchase from the operator topping the wallet up. Without this
    # a funding deposit is reported as revenue, which is the one number this
    # script exists to get right.
    price = args.price / 1e6
    paid = [s for s in found if abs(s["usdc"] - price) < 1e-9]
    other = [s for s in found if s not in paid]

    for sale in paid:
        print(f"  SALE  +{sale['usdc']:.2f} USDC  from {sale['payer']}  {sale['tx']}")
    for deposit in other:
        print(f"  (deposit, not a sale: +{deposit['usdc']:.2f} USDC from {deposit['payer']})")
    print(f"\n{len(paid)} sale(s) at {price:.2f} USDC = {sum(s['usdc'] for s in paid):.2f} USDC "
          f"in the last {args.blocks} blocks")

    if not args.follow:
        return 0

    seen = {sale["tx"] for sale in found}
    print("\nfollowing — new sales print as they land (Ctrl-C to stop)")
    while True:
        time.sleep(20)
        try:
            latest = int(rpc(url, "eth_blockNumber", []), 16)
            for sale in sales(url, contract, args.pay_to, head + 1, latest):
                if sale["tx"] in seen:
                    continue
                seen.add(sale["tx"])
                stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                kind = "SALE " if abs(sale["usdc"] - price) < 1e-9 else "deposit"
                print(f"[{stamp}] {kind} +{sale['usdc']:.2f} USDC from {sale['payer']}  {sale['tx']}")
            head = latest
        except SystemExit as exc:
            # A flaky public RPC should pause the watch, not end it.
            print(f"  (rpc hiccup: {exc})")


if __name__ == "__main__":
    raise SystemExit(main())
