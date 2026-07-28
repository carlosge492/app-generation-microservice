"""x402 payment gate.

CLAUDE.md §4: the fastlane build command must never run unless
`x402_payment_verified` is True in the supervisor payload. This module is the
only place that decision is made, so it can be audited in one file.
"""

from __future__ import annotations

from typing import Any


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
