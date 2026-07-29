"""x402 payment gate.

CLAUDE.md §4: the fastlane build command must never run unless
`x402_payment_verified` is True in the supervisor payload. This module is the
only place that decision is made, so it can be audited in one file.
"""

from __future__ import annotations

import secrets
from typing import Any, Protocol


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


class DevPaymentVerifier:
    """Accepts a shared secret. Stands in for a real x402 facilitator.

    Deliberately not a real settlement implementation — that means talking to a
    facilitator, checking a signature and an amount, and guarding against
    replay. This exists so the *gate* can be wired and tested end to end, and
    fails closed when no secret is configured, so a misconfigured deployment
    refuses payment rather than granting it.
    """

    def __init__(self, secret: str | None) -> None:
        self._secret = secret or ""

    def settle(self, header_value: str | None) -> bool:
        if not self._secret or not header_value:
            return False
        return secrets.compare_digest(header_value, self._secret)


def challenge(price: str = "0.50", asset: str = "USDC") -> dict[str, Any]:
    """The body accompanying a 402, telling the buyer how to pay."""
    return {
        "x402Version": 1,
        "accepts": [{
            "scheme": "exact",
            "amount": price,
            "asset": asset,
            "description": "One PRD compiled to a Flutter APK",
        }],
        "hint": f"retry with the {PAYMENT_HEADER} header",
    }
