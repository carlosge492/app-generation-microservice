"""x402 payment gate.

CLAUDE.md §4: the fastlane build command must never run unless
`x402_payment_verified` is True in the supervisor payload. This module is the
only place that decision is made, so it can be audited in one file.
"""

from __future__ import annotations

import secrets
from decimal import Decimal
from typing import Any, Protocol

from src.payments.facilitator import PrecheckResult, SettlementResult
from src.payments.eip3009 import (
    PaymentInvalid,
    TokenConfig,
    VerifiedPayment,
    verify_payment,
)
from src.payments.replay import InMemoryNonceStore, NonceStore


class PaymentNotVerified(RuntimeError):
    """Raised when a packaging step is attempted without a verified payment."""


def is_verified(payload: Any) -> bool:
    """True only for a literal boolean True.

    Truthy strings like "false" or "0" must not open the gate, so this is
    deliberately stricter than `bool(...)`.
    """
    if isinstance(payload, dict):
        payload = payload.get("x402_payment_verified", False)
    return payload is True


def require_verified(payload: Any) -> None:
    if not is_verified(payload):
        raise PaymentNotVerified(
            "x402_payment_verified is not True — refusing to run the build pipeline"
        )


# --------------------------------------------------------------------------- #
# Settling payment for an HTTP request
# --------------------------------------------------------------------------- #

PAYMENT_HEADER = "X-Payment"


class PaymentRequired(Exception):
    """No acceptable payment accompanied the request; answer 402."""

    def __init__(self, challenge: dict[str, Any]) -> None:
        super().__init__("payment required")
        self.challenge = challenge


class PaymentVerifier(Protocol):
    def settle(self, header_value: str | None) -> bool: ...


class Facilitator(Protocol):
    """Submits a verified authorization on-chain and reports what happened.

    Returns a `SettlementResult` rather than a bool deliberately. A bool has
    room for "settled" and "did not settle" and nowhere to put the transaction
    hash — which is the buyer's only proof of payment — or the difference
    between a definite refusal and an unknown outcome. An earlier version did
    return a bool, and the receipt was silently lost all the way to the API.
    """

    def settle(self, payment: VerifiedPayment) -> SettlementResult: ...

    def precheck(self, payment: VerifiedPayment) -> PrecheckResult: ...


class UnsettledFacilitator:
    """Records authorizations without submitting them.

    Verification proves the payer *signed* an authorization; only submitting it
    proves the money moved. With no facilitator configured the service is
    accepting signed promises, which is a deliberate deployment choice for a
    testnet or a trusted counterparty and a bad one otherwise. It is therefore
    reported by /healthz rather than left to be inferred, and the result carries
    no transaction hash because there is no transaction.
    """

    def __init__(self) -> None:
        self.pending: list[VerifiedPayment] = []

    def settle(self, payment: VerifiedPayment) -> SettlementResult:
        self.pending.append(payment)
        return SettlementResult(
            settled=True, reason="verification-only: not submitted on-chain"
        )

    def precheck(self, payment: VerifiedPayment) -> PrecheckResult:
        # Nothing to ask; there is no chain in this mode.
        return PrecheckResult(True)


class X402Verifier:
    """Real x402: EIP-712 signature recovery plus single-use nonces.

    Order matters. The signature is checked first, so a garbage header costs
    nothing and never touches the nonce store. The nonce is then claimed
    atomically — that claim is the moment the payment is spent, and it happens
    before any build is queued. Settlement runs last, because a facilitator
    rejection must not silently un-spend a nonce that a concurrent request has
    already been refused.
    """

    def __init__(
        self,
        token: TokenConfig,
        pay_to: str,
        min_value: int,
        nonces: NonceStore | None = None,
        facilitator: Facilitator | None = None,
        clock_skew: int = 0,
    ) -> None:
        self.token = token
        self.pay_to = pay_to
        self.min_value = min_value
        self.nonces = nonces if nonces is not None else InMemoryNonceStore()
        self.facilitator = facilitator or UnsettledFacilitator()
        self.clock_skew = clock_skew
        self.last_error: str | None = None
        # The buyer's receipt. Populated on success so the API can hand back
        # proof of payment rather than an unverifiable "trust me".
        self.last_transaction: str | None = None
        self.last_settlement: SettlementResult | None = None

    def settle(self, header_value: str | None) -> bool:
        self.last_error = None
        self.last_transaction = None
        self.last_settlement = None
        if not header_value:
            self.last_error = "no payment header"
            return False

        try:
            payment = verify_payment(
                header_value,
                token=self.token,
                pay_to=self.pay_to,
                min_value=self.min_value,
                clock_skew=self.clock_skew,
            )
        except PaymentInvalid as exc:
            self.last_error = str(exc)
            return False

        # Ask the facilitator before spending the nonce. Insufficient funds is
        # recoverable — the payer tops up and re-presents the same signature —
        # so burning their authorization for it would be needlessly destructive.
        precheck = getattr(self.facilitator, "precheck", None)
        if precheck is not None:
            advice = precheck(payment)
            if not advice.valid:
                self.last_error = (
                    f"payment could not be confirmed: {advice.reason}"
                    if not advice.unknown
                    else f"could not reach the facilitator ({advice.reason})"
                )
                return False

        auth = payment.authorization
        if not self.nonces.claim(auth.replay_key(self.token), auth.valid_before):
            # Valid signature, already spent. This is the replay case, and it is
            # worth distinguishing from a bad signature when reporting.
            self.last_error = "authorization has already been used"
            return False

        result = self.facilitator.settle(payment)
        self.last_settlement = result
        if not result.ok:
            # The nonce stays claimed on purpose: see replay.py.
            self.last_error = (
                # An unknown outcome is not a refusal, and saying so matters:
                # the buyer may have been charged for a build they did not get.
                f"settlement outcome unknown ({result.reason}) — "
                "the payment may have executed on-chain"
                if result.unknown
                else f"payment was not settled: {result.reason}"
            )
            return False

        self.last_transaction = result.transaction
        return True


class DevPaymentVerifier:
    """Shared-secret stand-in, for local development only.

    Retained so the service can be exercised without keys or a chain, and it
    fails closed with no secret configured. Never use it where money matters:
    it proves possession of a static string, which is replayable by anyone who
    ever sees one request.
    """

    def __init__(self, secret: str | None) -> None:
        self._secret = secret or ""
        self.last_error: str | None = None

    def settle(self, header_value: str | None) -> bool:
        if not self._secret or not header_value:
            self.last_error = "no payment header or no secret configured"
            return False
        ok = secrets.compare_digest(header_value, self._secret)
        self.last_error = None if ok else "shared secret mismatch"
        return ok


def human_amount(atomic: int, decimals: int) -> str:
    """The atomic price as a decimal string, for the human-readable field.

    Derived rather than configured. When the displayed price was its own
    parameter it kept the default it shipped with while the enforced price moved
    to $3.00 — the 402 advertised fifty cents and the signature it accepted was
    for six times that.
    """
    units = Decimal(atomic) / (Decimal(10) ** decimals)
    text = f"{units:.{decimals}f}"
    if "." not in text:
        return text
    # Drop trailing zeros so 3000000 reads as "3.00" not "3.000000", but keep at
    # least two places so a round price still looks like a price. Stripping is
    # confined to the fractional part: on a 0-decimal token "30" is thirty.
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.rstrip('0').ljust(2, '0')}"


DEFAULT_PAYMENT_TIMEOUT_SECONDS = 300


def challenge(
    *,
    token: TokenConfig | None = None,
    pay_to: str | None = None,
    max_amount_required: int | None = None,
    resource: str = "/builds",
    error: str | None = None,
    output_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The body accompanying a 402, telling the buyer exactly how to pay.

    This is x402 version 1, matching `specs/x402-specification-v1.md`, because
    v1 is what the facilitator settles — its `/supported` lists no other version.

    Every field name here is the spec's, not one that reads naturally. The first
    version of this function was written against our own client and the two
    agreed with each other rather than with the standard: `asset` carried the
    symbol "USDC" where the spec requires the token contract address, the
    required `maxTimeoutSeconds` was missing, `resource` was a path rather than a
    URL, and a human-readable `amount` of "3.00" sat where v2 defines `amount` as
    *atomic units* — a client reading it as specified would have signed for three
    millionths of a dollar. No third-party client could have paid this service.
    """
    accepts: dict[str, Any] = {"scheme": "exact"}

    if token is not None:
        accepts["network"] = token.network
    if max_amount_required is not None:
        accepts["maxAmountRequired"] = str(max_amount_required)
    if token is not None:
        # The contract address, per the spec — the buyer needs it to sign, and
        # this is the field they are told to read it from.
        accepts["asset"] = token.verifying_contract
    if pay_to is not None:
        accepts["payTo"] = pay_to

    accepts["resource"] = resource
    accepts["description"] = "One PRD compiled to a Flutter APK"
    if max_amount_required is not None and token is not None:
        # The price in dollars belongs in prose. It is not a protocol field: the
        # only spec field it could plausibly occupy is `amount`, which means
        # atomic units, and putting "3.00" there misreads as $0.000003.
        price = human_amount(max_amount_required, token.asset_decimals)
        accepts["description"] += f" (${price} USDC)"
    accepts["mimeType"] = "application/json"
    accepts["outputSchema"] = output_schema
    accepts["maxTimeoutSeconds"] = DEFAULT_PAYMENT_TIMEOUT_SECONDS
    if token is not None:
        # Scheme-specific: for `exact` on EVM this is the EIP-712 domain the
        # token signs under, which differs between mainnet and testnet USDC.
        accepts["extra"] = {"name": token.domain_name, "version": token.domain_version}

    body: dict[str, Any] = {
        "x402Version": 1,
        "accepts": [accepts],
        "hint": f"retry with the {PAYMENT_HEADER} header",
    }
    if error:
        # Why this particular attempt was refused. Distinguishing "expired" from
        # "already used" from "wrong recipient" is the difference between a
        # client that can retry correctly and one that cannot.
        body["error"] = error
    return body
