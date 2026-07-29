"""Facilitator client — turns a verified authorization into money.

Verification proves the payer signed an authorization. Settlement submits it to
the token contract so the transfer actually executes. Only the second one is
payment, and this module is the difference between the two.

The service blocks the `202 Accepted` on settlement completing: a build is
expensive, and starting one on the strength of a signature alone means an
unfunded wallet gets free work. That costs the buyer a few seconds of latency
and costs us nothing we were not already spending.

Three properties are deliberate rather than incidental:

**Retrying is safe, timing out is not.** An EIP-3009 nonce is consumed on-chain,
so submitting the same authorization twice reverts rather than charging twice —
retrying a *connection* failure is therefore sound. A timeout is different: the
facilitator may have broadcast successfully and simply not answered in time, so
the outcome is genuinely unknown. That case fails closed and says so, because
the alternative is granting a build we may never be paid for.

**Ambiguity is reported, not smoothed over.** `SettlementResult` distinguishes
settled, refused and unknown. Collapsing unknown into failure would be a lie
about whether the buyer was charged.

**No retry after a definite refusal.** If the facilitator says the payer has
insufficient funds, asking again produces the same answer and only delays the
402.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from src.payments.eip3009 import TokenConfig, VerifiedPayment

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettlementResult:
    """Outcome of submitting an authorization.

    `unknown` is the important one: the request did not complete, the
    authorization may or may not have executed on-chain, and no amount of local
    reasoning can decide which. It is treated as a failure for the purpose of
    granting service, and recorded distinctly for the purpose of reconciliation.
    """

    settled: bool
    unknown: bool = False
    transaction: str | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.settled and not self.unknown


def payment_requirements(
    token: TokenConfig, pay_to: str, price_atomic: int, resource: str = "/builds"
) -> dict[str, Any]:
    """The requirements object a facilitator checks the payload against.

    Sent alongside the payload so the facilitator can confirm the authorization
    matches what we actually asked for, rather than taking our word for it.
    """
    return {
        "scheme": "exact",
        "network": token.network,
        "maxAmountRequired": str(price_atomic),
        "resource": resource,
        "description": "One PRD compiled to a Flutter APK",
        "mimeType": "application/json",
        "payTo": pay_to,
        "asset": token.verifying_contract,
        "maxTimeoutSeconds": 120,
        "extra": {"name": token.domain_name, "version": token.domain_version},
    }


class HttpFacilitator:
    """POSTs a verified payload to an x402 facilitator for on-chain execution.

    The wire format follows the x402 facilitator convention (`/settle` taking
    `paymentPayload` + `paymentRequirements`, answering with `success` and a
    transaction hash). Facilitators differ in details, so the response reader is
    tolerant about field names and strict about the one thing that matters:
    nothing counts as settled unless the response says so explicitly.
    """

    def __init__(
        self,
        base_url: str,
        token: TokenConfig,
        pay_to: str,
        price_atomic: int,
        api_key: str | None = None,
        timeout: float = 60.0,
        retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.pay_to = pay_to
        self.price_atomic = price_atomic
        self.timeout = timeout
        self.retries = retries
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.Client(timeout=timeout)

    # -- protocol ----------------------------------------------------------- #

    def settle(self, payment: VerifiedPayment) -> SettlementResult:
        body = {
            "x402Version": 1,
            "paymentPayload": payment.payload,
            "paymentRequirements": payment_requirements(
                self.token, self.pay_to, self.price_atomic
            ),
        }

        last: SettlementResult | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self._client.post(
                    f"{self.base_url}/settle", json=body, headers=self._headers
                )
            except httpx.TimeoutException as exc:
                # The facilitator may have broadcast and not answered. Retrying
                # is still safe -- a duplicate submission reverts on the spent
                # nonce -- but if every attempt times out the outcome stands
                # unknown rather than refused.
                last = SettlementResult(False, unknown=True, reason=f"timeout: {exc}")
                log.warning("x402 settle attempt %s timed out", attempt + 1)
                continue
            except httpx.HTTPError as exc:
                last = SettlementResult(False, unknown=True, reason=f"transport: {exc}")
                log.warning("x402 settle attempt %s failed: %s", attempt + 1, exc)
                continue

            result = self._read(response)
            if result.ok or not result.unknown:
                # Settled, or definitively refused. Asking again would only
                # repeat the same answer.
                return result
            last = result

        return last or SettlementResult(False, unknown=True, reason="no attempt completed")

    # -- response handling -------------------------------------------------- #

    def _read(self, response: httpx.Response) -> SettlementResult:
        if response.status_code >= 500:
            # The facilitator broke, not the payment. Worth another attempt.
            return SettlementResult(
                False, unknown=True, reason=f"facilitator {response.status_code}"
            )

        try:
            body = response.json()
        except ValueError:
            return SettlementResult(
                False, unknown=True,
                reason=f"unreadable response ({response.status_code})",
            )
        if not isinstance(body, dict):
            return SettlementResult(False, unknown=True, reason="response was not an object")

        if response.status_code >= 400:
            return SettlementResult(
                False,
                reason=str(body.get("errorReason") or body.get("error")
                           or f"facilitator {response.status_code}"),
            )

        # Settlement is affirmative-only: an ambiguous body is not a payment.
        success = body.get("success")
        transaction = body.get("transaction") or body.get("txHash")
        if success is True:
            return SettlementResult(True, transaction=transaction)
        if success is False:
            return SettlementResult(
                False, reason=str(body.get("errorReason") or "facilitator refused")
            )
        return SettlementResult(
            False, unknown=True, reason="response did not state success either way"
        )
