"""EIP-712 / EIP-3009 payment authorization verification.

An x402 `exact` payment on an EVM chain is an EIP-3009
`TransferWithAuthorization` message: the payer signs, off-chain, an
authorization for the token contract to move a fixed amount to a fixed
recipient. Anyone holding that signature can submit it on-chain.

What this module establishes, cryptographically:

  * the signature recovers to the address claiming to authorise the transfer
  * the money is going to *our* address, in the expected asset, on the expected
    chain, for at least the price we asked
  * the authorization is inside its validity window
  * the nonce has never been accepted by us before

What it does NOT establish, and what no amount of local verification can:

  * that the payer holds the balance
  * that the transfer has actually been executed on-chain

Those require submitting the authorization through a facilitator or RPC. A
verified-but-unsettled authorization is a *promise* backed by a signature, not
money received — see `Facilitator` in x402.py. Conflating the two would be the
most expensive mistake available here, so the two steps are kept apart and named
differently throughout.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address

X402_VERSION = 1
SCHEME_EXACT = "exact"

EIP712_DOMAIN_FIELDS = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

TRANSFER_WITH_AUTHORIZATION_FIELDS = [
    {"name": "from", "type": "address"},
    {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"},
]


class PaymentInvalid(Exception):
    """The payment payload is malformed, mis-addressed, expired or unsigned."""


@dataclass(frozen=True)
class TokenConfig:
    """Everything needed to reconstruct the payer's EIP-712 domain.

    The domain must match the token contract exactly — `name` and `version` are
    read from the deployed contract, not chosen by us. USDC on most chains is
    ("USDC", "2"), but native-vs-bridged deployments differ, and a mismatch
    silently makes every signature fail to recover to the right address.
    """

    chain_id: int
    verifying_contract: str
    domain_name: str = "USDC"
    domain_version: str = "2"
    network: str = "base-sepolia"
    asset_decimals: int = 6

    def domain(self) -> dict[str, Any]:
        return {
            "name": self.domain_name,
            "version": self.domain_version,
            "chainId": self.chain_id,
            "verifyingContract": to_checksum_address(self.verifying_contract),
        }


@dataclass(frozen=True)
class Authorization:
    sender: str
    recipient: str
    value: int
    valid_after: int
    valid_before: int
    nonce: bytes

    def replay_key(self, token: TokenConfig) -> str:
        """Identity for replay purposes.

        EIP-3009 nonces are unique per (token contract, authoriser) — the same
        random 32 bytes from a different payer, or for a different token, is a
        different authorization. Keying on the nonce alone would let one payer's
        spent nonce block another payer's valid one.
        """
        return ":".join((
            str(token.chain_id),
            to_checksum_address(token.verifying_contract),
            to_checksum_address(self.sender),
            "0x" + self.nonce.hex(),
        ))


@dataclass(frozen=True)
class VerifiedPayment:
    authorization: Authorization
    token: TokenConfig
    raw_header: str
    payload: dict[str, Any]


def _decode_header(header_value: str) -> dict[str, Any]:
    """x402 sends the payload base64-encoded; tolerate raw JSON as well."""
    text = header_value.strip()
    if not text:
        raise PaymentInvalid("empty payment header")
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise PaymentInvalid(f"payment header is not valid base64: {exc}") from exc
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PaymentInvalid(f"payment header is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise PaymentInvalid("payment payload must be a JSON object")
    return decoded


def _as_int(value: Any, field: str) -> int:
    """Amounts and timestamps arrive as JSON strings to survive uint256."""
    try:
        return int(str(value), 0) if str(value).startswith("0x") else int(str(value))
    except (TypeError, ValueError) as exc:
        raise PaymentInvalid(f"{field} is not an integer: {value!r}") from exc


def _as_nonce(value: Any) -> bytes:
    try:
        raw = bytes.fromhex(str(value).removeprefix("0x"))
    except ValueError as exc:
        raise PaymentInvalid(f"nonce is not hex: {value!r}") from exc
    if len(raw) != 32:
        raise PaymentInvalid(f"nonce must be 32 bytes, got {len(raw)}")
    return raw


def parse_authorization(payload: dict[str, Any]) -> Authorization:
    if payload.get("x402Version") != X402_VERSION:
        raise PaymentInvalid(f"unsupported x402Version: {payload.get('x402Version')!r}")
    if payload.get("scheme") != SCHEME_EXACT:
        raise PaymentInvalid(f"unsupported scheme: {payload.get('scheme')!r}")

    inner = payload.get("payload")
    if not isinstance(inner, dict):
        raise PaymentInvalid("payload.payload missing")
    auth = inner.get("authorization")
    if not isinstance(auth, dict):
        raise PaymentInvalid("payload.authorization missing")

    try:
        sender = to_checksum_address(auth["from"])
        recipient = to_checksum_address(auth["to"])
    except (KeyError, ValueError) as exc:
        raise PaymentInvalid(f"authorization addresses invalid: {exc}") from exc

    return Authorization(
        sender=sender,
        recipient=recipient,
        value=_as_int(auth.get("value"), "value"),
        valid_after=_as_int(auth.get("validAfter", 0), "validAfter"),
        valid_before=_as_int(auth.get("validBefore"), "validBefore"),
        nonce=_as_nonce(auth.get("nonce")),
    )


def typed_data(auth: Authorization, token: TokenConfig) -> dict[str, Any]:
    return {
        "types": {
            "EIP712Domain": EIP712_DOMAIN_FIELDS,
            "TransferWithAuthorization": TRANSFER_WITH_AUTHORIZATION_FIELDS,
        },
        "primaryType": "TransferWithAuthorization",
        "domain": token.domain(),
        "message": {
            "from": auth.sender,
            "to": auth.recipient,
            "value": auth.value,
            "validAfter": auth.valid_after,
            "validBefore": auth.valid_before,
            "nonce": auth.nonce,
        },
    }


def recover_signer(auth: Authorization, token: TokenConfig, signature: str) -> str:
    signable = encode_typed_data(full_message=typed_data(auth, token))
    try:
        return to_checksum_address(
            Account.recover_message(signable, signature=signature)
        )
    except Exception as exc:  # bad length, bad v, non-hex, ...
        raise PaymentInvalid(f"signature could not be recovered: {exc}") from exc


def verify_payment(
    header_value: str,
    *,
    token: TokenConfig,
    pay_to: str,
    min_value: int,
    now: int | None = None,
    clock_skew: int = 0,
) -> VerifiedPayment:
    """Cryptographically verify an x402 payment authorization.

    Raises `PaymentInvalid` for anything wrong. Does not consume the nonce —
    replay protection is a separate, atomic step, so that verification stays a
    pure function and the claim can be made at exactly the moment service is
    granted.
    """
    payload = _decode_header(header_value)

    network = payload.get("network")
    if network is not None and network != token.network:
        raise PaymentInvalid(
            f"payment is for network {network!r}, this service accepts {token.network!r}"
        )

    auth = parse_authorization(payload)

    expected = to_checksum_address(pay_to)
    if auth.recipient != expected:
        # The signature may be perfectly valid — for somebody else.
        raise PaymentInvalid(
            f"payment is addressed to {auth.recipient}, not {expected}"
        )

    if auth.value < min_value:
        raise PaymentInvalid(
            f"payment authorises {auth.value}, price is {min_value}"
        )

    moment = int(time.time()) if now is None else now
    if auth.valid_before <= moment - clock_skew:
        raise PaymentInvalid("authorization has expired")
    if auth.valid_after > moment + clock_skew:
        raise PaymentInvalid("authorization is not valid yet")

    inner = payload["payload"]
    signature = inner.get("signature")
    if not isinstance(signature, str) or not signature:
        raise PaymentInvalid("payload.signature missing")

    signer = recover_signer(auth, token, signature)
    if signer != auth.sender:
        # Somebody signed an authorization claiming to be a different payer.
        raise PaymentInvalid(
            f"signature recovers to {signer}, which is not the declared payer {auth.sender}"
        )

    return VerifiedPayment(
        authorization=auth, token=token, raw_header=header_value, payload=payload
    )
