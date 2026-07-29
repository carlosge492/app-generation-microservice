"""Poll the facilitator until the payer's testnet USDC arrives.

No RPC endpoint or block explorer needed: /verify already answers
`invalid_exact_evm_insufficient_balance` for an unfunded payer, so the moment it
stops saying that, the money has landed. Reusing the oracle we already trust
beats introducing a second source of truth that could disagree with it.
"""
import json, os, pathlib, sys, time
from eth_account import Account
from eth_account.messages import encode_typed_data
from src.payments.eip3009 import Authorization, TokenConfig, typed_data
from src.payments.facilitator import HttpFacilitator, PrecheckResult

TOKEN = TokenConfig(chain_id=84532,
                    verifying_contract="0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                    network="base-sepolia")
keys = json.loads(pathlib.Path(".x402-testnet.json").read_text())
payer = Account.from_key(keys["payer"]["private_key"])
pay_to = keys["receiver"]["address"]


class _P:
    def __init__(self, payload): self.payload = payload


def probe():
    auth = Authorization(sender=payer.address, recipient=pay_to, value=500_000,
                         valid_after=0, valid_before=int(time.time()) + 600,
                         nonce=os.urandom(32))
    sig = payer.sign_message(
        encode_typed_data(full_message=typed_data(auth, TOKEN))).signature.to_0x_hex()
    payload = {"x402Version": 1, "scheme": "exact", "network": "base-sepolia",
               "payload": {"signature": sig, "authorization": {
                   "from": auth.sender, "to": auth.recipient, "value": "500000",
                   "validAfter": "0", "validBefore": str(auth.valid_before),
                   "nonce": "0x" + auth.nonce.hex()}}}
    fac = HttpFacilitator("https://facilitator.payai.network", TOKEN, pay_to, 500_000,
                          timeout=45)
    return fac.precheck(_P(payload))


print(f"payer   : {payer.address}")
print(f"receiver: {pay_to}")
result = probe()
if result.valid:
    print("\nFUNDED — the facilitator says this authorization would settle.")
    sys.exit(0)
print(f"\nnot funded yet: {result.reason}")
sys.exit(1)
