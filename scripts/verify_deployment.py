"""Check a deployed service is actually sellable before pointing buyers at it.

    poetry run python scripts/verify_deployment.py https://your-host:8000

Every failure this looks for is one a deployment can have while looking healthy:
a container that came up, answers HTTP, and quietly takes money it cannot settle
or loses builds on restart. `/healthz` was built to make that diagnosable, so
this reads it and refuses to call the deployment ready when it says the wrong
thing.

What it proves without spending anything:

  * payment is the real x402 verifier, not the dev shared secret and not the
    refusing fallback a misconfigured deployment falls back to;
  * settlement is on-chain, so the service is not accepting signed promises;
  * the job store and queue are Redis, so a restart mid-build does not lose a
    build somebody paid for;
  * an unpaid request is refused with a challenge, and a buyer who forges
    `x402_payment_verified` in their own PRD is still refused — that field is
    buyer-supplied, and trusting it would give the app away.

`--pay` additionally buys a real build with the funded testnet key in
`.x402-testnet.json`, waits for the APK and downloads it. That spends testnet
USDC and takes as long as a build takes, which is why it is opt-in — but it is
the only check that proves the deployment can do the thing it charges for.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from src.payments.x402 import PAYMENT_HEADER  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PRD_PATH = ROOT / "examples" / "todo_app.prd.json"
KEYS_PATH = ROOT / ".x402-testnet.json"
# Networks where a payment is real and irreversible. Named rather than inferred
# from "is it not sepolia", so a new testnet does not silently become one.
MAINNET_NETWORKS = {"base", "ethereum", "polygon", "avalanche", "arbitrum", "optimism"}

# A token whose chain, contract and EIP-712 domain do not agree rejects every
# signature a correct buyer produces. These are not recalled — they were read
# from the facilitator's own discovery listings, where 80 of 80 live
# Base-mainnet services agree on both the contract and the domain. This table is
# the single source: `deploy/switch_network.sh` writes the same values into a
# deployment, and a test holds the two together.
KNOWN_NETWORKS = {
    "base": {
        "chain_id": 8453,
        "contract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "domain_name": "USD Coin",
    },
    "base-sepolia": {
        "chain_id": 84532,
        "contract": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "domain_name": "USDC",
    },
}


class Failure(Exception):
    """A deployment problem, phrased for whoever has to fix it."""


def log(message: str) -> None:
    print(f"  {message}", flush=True)


def check_health(base: str, expect_network: str) -> dict:
    try:
        response = httpx.get(f"{base}/healthz", timeout=30)
    except httpx.HTTPError as exc:
        raise Failure(f"no answer from {base}/healthz: {exc}") from exc
    if response.status_code != 200:
        raise Failure(f"{base}/healthz returned {response.status_code}")
    health = response.json()

    problems: list[str] = []
    if health.get("payment_mode") != "x402-eip3009":
        problems.append(
            f"payment_mode is {health.get('payment_mode')!r}, not 'x402-eip3009'. "
            f"A deployment missing X402_TOKEN_CONTRACT/X402_CHAIN_ID/X402_PAY_TO "
            f"falls back to refusing every payment, or worse to the dev shared "
            f"secret, which sells builds to anyone who guesses a string."
        )
    if health.get("settlement") != "on-chain":
        problems.append(
            "settlement is 'verification-only': X402_FACILITATOR_URL is unset, so "
            "the service accepts authorizations it never submits. Buyers get "
            "builds; the money never moves."
        )
    if health.get("network") != expect_network:
        problems.append(
            f"network is {health.get('network')!r}, expected {expect_network!r}. "
            f"Paying on the wrong chain fails at the facilitator, and a mainnet "
            f"deployment reached by accident charges real money."
        )
    if not health.get("multi_process_safe"):
        problems.append(
            "multi_process_safe is false: REDIS_URL is unset, so replay "
            "protection holds only within one process and the job store is in "
            "memory."
        )
    if not health.get("durable_execution"):
        problems.append(
            "durable_execution is false: a restart or a killed worker loses "
            "builds that have already been paid for."
        )
    token = health.get("token") or {}
    expected = KNOWN_NETWORKS.get(str(health.get("network")))
    if expected and token:
        for field, want in expected.items():
            got = token.get(field)
            if isinstance(want, str) and isinstance(got, str):
                matches = got.lower() == want.lower()
            else:
                matches = got == want
            if not matches:
                problems.append(
                    f"token {field} is {got!r} but {health.get('network')} uses "
                    f"{want!r}. Every buyer signature is computed over the token's "
                    f"real domain, so a mismatch here rejects all of them — which "
                    f"reads as a broken deployment rather than a config error."
                )

    if health.get("generator_ready") is False:
        problems.append(
            f"generator_ready is false: the deployment is set to the "
            f"{health.get('generator')!r} generator but cannot run it — every "
            f"build would fail after the payment had already settled."
        )
    if health.get("builds_rate_limit") in (None, "unlimited"):
        problems.append(
            "builds_rate_limit is unlimited: POST /builds verifies payment over "
            "two facilitator round trips from a synchronous endpoint, so a flood "
            "of junk authorizations exhausts the thread pool and the service "
            "stops answering the buyers who did pay."
        )
    if problems:
        raise Failure("\n  - ".join(["/healthz reports a service that is not sellable:"] + problems))
    return health


def check_payment_is_required(base: str) -> None:
    """Unpaid requests are refused, and the refusal tells a machine how to pay."""
    prd = json.loads(PRD_PATH.read_text(encoding="utf-8"))

    response = httpx.post(f"{base}/builds", json=prd, timeout=30)
    if response.status_code != 402:
        raise Failure(
            f"an unpaid POST /builds returned {response.status_code}, not 402. "
            f"This deployment is giving builds away."
        )
    quote = response.json()
    hint = json.dumps(quote)
    if PAYMENT_HEADER not in hint:
        raise Failure(
            f"the 402 does not name the {PAYMENT_HEADER} header, so an M2M buyer "
            f"cannot discover how to pay from the challenge alone"
        )

    # The quote states the price twice, and a buyer agent may show one figure and
    # sign the other. Checked on the live service because the two come from
    # different places in the config and only the deployment has both.
    terms = quote.get("accepts", [{}])[0]
    advertised, enforced = terms.get("amount"), terms.get("maxAmountRequired")
    if advertised is not None and enforced is not None:
        decimals = 6  # USDC, on every network this deployment ships for
        if Decimal(advertised) * (10 ** decimals) != Decimal(enforced):
            raise Failure(
                f"the 402 advertises {advertised} USDC but enforces {enforced} "
                f"atomic units ({Decimal(enforced) / 10 ** decimals}). A buyer "
                f"that displays the first and signs the second pays a different "
                f"price than the one it was quoted."
            )

    # The gate's whole reason for existing: `x402_payment_verified` is a field on
    # a buyer-supplied document, and a buyer who sets it must still be refused.
    forged = dict(prd, x402_payment_verified=True)
    response = httpx.post(f"{base}/builds", json=forged, timeout=30)
    if response.status_code != 402:
        raise Failure(
            f"a PRD claiming its own payment was accepted with "
            f"{response.status_code}. Buyers can self-certify on this deployment "
            f"and take builds without paying."
        )


def check_a_real_signature_verifies(base: str, keys_path: Path = KEYS_PATH) -> str:
    """Prove the chain config accepts a correctly-signed authorization, free.

    The failure this catches is the one that presents as a broken service: an
    EIP-712 domain that does not match the token's, so every buyer signature
    fails to recover and nothing works while `/healthz` looks fine. Running it
    costs nothing because the service verifies the signature *before* it asks
    the facilitator to settle, so a key with no balance separates the two:

      rejected at the signature  -> the config is wrong; no buyer can ever pay
      rejected at settlement     -> the config is right, this wallet is empty

    That makes a mainnet deployment checkable before a single real payment,
    which is otherwise a chicken-and-egg problem: you cannot learn whether
    mainnet works without spending on mainnet.
    """
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    from src.payments.eip3009 import Authorization, TokenConfig, typed_data

    if not keys_path.exists():
        raise Failure(f"the signature check needs any funded-or-not key in {keys_path.name}")
    payer = Account.from_key(json.loads(keys_path.read_text(encoding="utf-8"))["payer"]["private_key"])

    prd = json.loads(PRD_PATH.read_text(encoding="utf-8"))
    terms = httpx.post(f"{base}/builds", json=prd, timeout=30).json()["accepts"][0]
    token = TokenConfig(
        chain_id=KNOWN_NETWORKS[terms["network"]]["chain_id"],
        verifying_contract=terms["asset"],
        domain_name=terms["extra"]["name"],
        domain_version=terms["extra"]["version"],
        network=terms["network"],
    )
    auth = Authorization(
        sender=payer.address, recipient=terms["payTo"],
        value=int(terms["maxAmountRequired"]), valid_after=0,
        valid_before=int(time.time()) + 600, nonce=secrets.token_bytes(32),
    )
    header = json.dumps({
        "x402Version": 1, "scheme": "exact", "network": token.network,
        "payload": {
            "signature": payer.sign_message(
                encode_typed_data(full_message=typed_data(auth, token))
            ).signature.to_0x_hex(),
            "authorization": {
                "from": auth.sender, "to": auth.recipient, "value": str(auth.value),
                "validAfter": "0", "validBefore": str(auth.valid_before),
                "nonce": "0x" + auth.nonce.hex(),
            },
        },
    })

    response = httpx.post(
        f"{base}/builds", json=prd, headers={PAYMENT_HEADER: header}, timeout=60
    )
    if response.status_code == 202:
        # Only reachable if this key really is funded on this network, which is
        # the normal testnet case — the payment stands and a build is running.
        return "accepted and paid for a build"

    reason = str(response.json().get("error") or "")
    if any(word in reason.lower() for word in
           ("recover", "signature", "domain", "signer", "malformed")):
        raise Failure(
            f"a correctly-signed authorization was rejected at the signature: "
            f"{reason}. The EIP-712 domain, chain id or token contract does not "
            f"match the deployed token, so no buyer can pay this service — it "
            f"fails closed and presents as broken rather than misconfigured."
        )
    return f"verified the signature and refused on funds ({reason[:60]})"


def buy_a_build(base: str, health: dict, timeout: float, keys_path: Path = KEYS_PATH) -> Path:
    """Pay for real and wait for the APK. Spends testnet USDC."""
    import secrets

    from eth_account import Account
    from eth_account.messages import encode_typed_data

    from src.payments.eip3009 import Authorization, TokenConfig, typed_data

    if not keys_path.exists():
        raise Failure(
            f"--pay needs a funded payer key in {keys_path.name}; see the "
            f"README on the testnet setup, or pass --payer-keys"
        )
    keys = json.loads(keys_path.read_text(encoding="utf-8"))
    payer = Account.from_key(keys["payer"]["private_key"])

    # Every chain parameter comes out of the deployment's own 402, never out of
    # this script's environment. Signing what we assume rather than what the
    # service asks for is how a check like this goes green against a service no
    # real buyer could use — and a buyer has nothing but the challenge either,
    # so reading it is also a test that the challenge is actionable.
    prd = json.loads(PRD_PATH.read_text(encoding="utf-8"))
    body = httpx.post(f"{base}/builds", json=prd, timeout=30).json()
    try:
        terms = body["accepts"][0]
        pay_to = terms["payTo"]
        price = int(terms["maxAmountRequired"])
        network = terms["network"]
        # Spec fields only. This script used to read `chainId` and
        # `verifyingContract`, which the spec does not define and the service
        # invented — so the pair agreed with each other while no third-party
        # client could pay at all. The chain id comes from a registry keyed on
        # the network name, which is what a real client has to do too.
        token = TokenConfig(
            chain_id=KNOWN_NETWORKS[network]["chain_id"],
            verifying_contract=terms["asset"],
            domain_name=terms["extra"]["name"],
            domain_version=terms["extra"]["version"],
            network=network,
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise Failure(
            f"the 402 challenge is missing what a buyer needs to sign ({exc}). "
            f"An M2M client has nothing but this body: {json.dumps(body)[:400]}"
        ) from exc
    auth = Authorization(
        sender=payer.address, recipient=pay_to, value=price,
        valid_after=0, valid_before=int(time.time()) + 600, nonce=secrets.token_bytes(32),
    )
    signature = payer.sign_message(
        encode_typed_data(full_message=typed_data(auth, token))
    ).signature.to_0x_hex()
    header = json.dumps({
        "x402Version": 1, "scheme": "exact", "network": token.network,
        "payload": {
            "signature": signature,
            "authorization": {
                "from": auth.sender, "to": auth.recipient, "value": str(price),
                "validAfter": "0", "validBefore": str(auth.valid_before),
                "nonce": "0x" + auth.nonce.hex(),
            },
        },
    })

    log(f"paying {price} atomic units of {terms.get('asset', 'the token')} to {pay_to}")
    response = httpx.post(
        f"{base}/builds",
        json=prd,
        headers={PAYMENT_HEADER: header},
        timeout=180,  # blocks until the payment settles on-chain
    )
    if response.status_code != 202:
        raise Failure(
            f"a signed, funded payment was refused with {response.status_code}: "
            f"{response.text[:400]}"
        )
    job_id = response.json()["id"]
    log(f"accepted as job {job_id}; building")

    deadline = time.time() + timeout
    state = "unknown"
    while time.time() < deadline:
        status = httpx.get(f"{base}/builds/{job_id}", timeout=30).json()
        state = status.get("status", "unknown")
        if state == "succeeded":
            break
        if state == "failed":
            raise Failure(
                f"the build failed on the deployment, after the money moved: "
                f"{status.get('failure') or json.dumps(status)[:400]}"
            )
        time.sleep(10)
    else:
        raise Failure(
            f"the build was still {state!r} after {timeout:.0f}s. The money moved; "
            f"check the worker logs."
        )

    apk = httpx.get(f"{base}/builds/{job_id}/apk", timeout=300, follow_redirects=True)
    if apk.status_code != 200:
        raise Failure(
            f"the build finished but the APK download returned {apk.status_code}. "
            f"A buyer has paid and cannot collect."
        )
    out = ROOT / "generated_apps" / f"deployment_check_{job_id}.apk"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(apk.content)
    if not apk.content.startswith(b"PK"):
        raise Failure(f"what came back is not a zip/APK: {out}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="base URL of the deployment, e.g. https://host:8000")
    parser.add_argument("--network", default="base-sepolia",
                        help="the network the deployment is expected to charge on; "
                             "a mismatch is treated as a failure rather than a note")
    parser.add_argument("--pay", action="store_true",
                        help="buy a real build, signing with the payer key file")
    parser.add_argument("--payer-keys", type=Path, default=KEYS_PATH,
                        help="JSON holding the payer's private key. Defaults to the "
                             "testnet file, which has no mainnet funds — a mainnet "
                             "run needs its own file and its own key")
    parser.add_argument("--yes-spend-real-money", action="store_true",
                        help="required by --pay on a mainnet network, where the "
                             "payment is real USDC and nothing can undo it")
    parser.add_argument("--build-timeout", type=float, default=1800)
    args = parser.parse_args()

    # A testnet run is free and repeatable; a mainnet run moves money that no
    # amount of retrying gets back. The flag exists so that spending it is a
    # thing someone typed, never a thing a command did by default.
    if args.pay and args.network in MAINNET_NETWORKS and not args.yes_spend_real_money:
        print(f"\nRefusing: --pay on {args.network} spends real USDC. Re-run with "
              f"--yes-spend-real-money if that is what you mean.")
        return 1
    if args.pay and args.network in MAINNET_NETWORKS and args.payer_keys == KEYS_PATH:
        print(f"\nRefusing: --pay on {args.network} would sign with "
              f"{KEYS_PATH.name}, the throwaway testnet key. Pass --payer-keys "
              f"pointing at a mainnet payer.")
        return 1

    base = args.url.rstrip("/")
    print(f"\nDeployment check — {base}\n" + "=" * 61)
    try:
        health = check_health(base, args.network)
        log(f"payment    {health['payment_mode']} on {health['network']}, "
            f"settlement {health['settlement']}")
        log(f"durability job store {health['job_store']}, "
            f"durable_execution {health['durable_execution']}")
        log(f"throttling  {health['builds_rate_limit']} per address on /builds")
        log(f"builds     {health['generator']} generator, "
            f"{health['build_mode']} mode, "
            f"embedded worker {health['embedded_worker']}")

        check_payment_is_required(base)
        log("refused    an unpaid request, and a PRD that certified its own payment")

        # Costs nothing and is the only check that exercises the chain config
        # against a real signature, so it runs by default rather than behind
        # --pay. On mainnet it is the difference between a deployment believed
        # to work and one shown to.
        if args.payer_keys.exists():
            log(f"signature  {check_a_real_signature_verifies(base, args.payer_keys)}")

        apk = None
        if args.pay:
            apk = buy_a_build(base, health, args.build_timeout, args.payer_keys)
    except Failure as failure:
        print(f"\nFAILED: {failure}")
        return 1

    print("\n" + "=" * 61)
    print("PASSED: the deployment is configured to sell builds.")
    print(f"  settles   on-chain on {health['network']}")
    print(f"  survives  a restart ({health['job_store']} job store and queue)")
    print("  refuses   unpaid and self-certified requests")
    if apk is not None:
        print(f"  delivered {apk.name} ({apk.stat().st_size / 1e6:.0f} MB) for a real payment")
    else:
        print("  not run   the paid end-to-end build (--pay)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
