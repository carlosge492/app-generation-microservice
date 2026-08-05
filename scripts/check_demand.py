"""Has anyone other than us actually used this service?

The decision this exists to answer is whether to keep paying for the box, so it
measures the top of the funnel rather than the bottom. Sales are the wrong
trigger: a free preview needs no wallet, no USDC and no signature, so if nobody
runs one, nobody was ever going to pay. A preview run by a stranger is the
earliest honest evidence of demand, and its absence is the earliest honest
evidence of none.

Reads the proxy's access log over SSH, because that is the one record that
counts requests nobody completed — a visitor who read the landing page and left
never touches Redis or the job store.

    poetry run python scripts/check_demand.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter

# Addresses whose traffic is ours and must not be counted as demand: this
# machine's own checks, and the known monitors that probe on a schedule.
OUR_AGENTS = (
    "python-httpx",          # every script in this repo
    "WindowsPowerShell",
    "nohumans.directory",    # listing liveness probe, every 15 minutes
    "x402-observer",
    "MCPWitness",
    "mcpgrade-probe",
    "io.verifymcp",
    "flowstacks-mcp-conformance",
    "loop-mcp-catalog-fetch",
    "agent-tools.cloud",
    "l9scan",                # vulnerability scanner noise
)

INTERESTING = ("/preview", "/validate", "/mcp", "/builds", "/llms.txt", "/")

REMOTE_SCRIPT = r"""
cd /opt/appgen
docker compose logs caddy --no-log-prefix 2>/dev/null | grep '"request"'
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="root@37.27.249.33")
    parser.add_argument("--key", default=None, help="ssh identity file")
    args = parser.parse_args()

    command = ["ssh"]
    if args.key:
        command += ["-i", args.key]
    command += [args.host, REMOTE_SCRIPT]

    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(f"could not read the log: {result.stderr[:200]}")
        return 1

    ours = Counter()
    theirs: list[tuple[str, str, str]] = []
    for line in result.stdout.splitlines():
        start = line.find('{"request"')
        if start == -1:
            continue
        try:
            entry = json.loads(line[start:])
        except ValueError:
            continue
        request = entry.get("request", {})
        uri = str(request.get("uri", "")).split("?")[0]
        if not any(uri == route or uri.startswith(route + "/") for route in INTERESTING):
            continue
        agent = (request.get("headers", {}).get("User-Agent") or ["-"])[0]
        if any(mine in agent for mine in OUR_AGENTS):
            ours[agent.split("/")[0]] += 1
            continue
        theirs.append((request.get("remote_ip", "?"), request.get("method", "?"), uri))

    previews = [t for t in theirs if t[2] == "/preview"]
    print(f"requests from us or known monitors : {sum(ours.values())}")
    print(f"requests from everyone else        : {len(theirs)}")
    print(f"  ...of which free previews        : {len(previews)}")
    print()

    if theirs:
        print("unattributed traffic, most recent last:")
        for ip, method, uri in theirs[-25:]:
            print(f"  {method:5} {uri:24} from {ip}")
    print()

    if previews:
        print("SOMEONE RAN A FREE PREVIEW — that is the demand signal. Keep going.")
        return 0
    print("No preview run by anyone but us. The free path costs nothing to try,")
    print("so nobody trying it is the answer, not a reason to wait longer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
