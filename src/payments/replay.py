"""Replay protection for payment authorizations.

An EIP-3009 authorization is a bearer instrument: the signature is valid until
the nonce is consumed on-chain, and anyone holding it can present it. Between
our accepting one and its settling on-chain there is a window in which the same
signature can be submitted again — so the service must refuse a nonce it has
already honoured, without waiting for the chain to say so.

Two properties matter more than they look:

**The claim is atomic.** Checking "have I seen this?" and then recording it is a
time-of-check/time-of-use race: two concurrent requests bearing the same
signature both pass the check, and both get a build. `claim` does both under one
lock and returns whether *this* caller was the first, so exactly one wins.

**A claim is never released.** If the build subsequently fails, the nonce stays
spent. Releasing it would mean a failed build hands back a reusable
authorization, and "make the build fail" is not a difficult thing for a
determined caller to arrange. Refunds are a business decision, not a
concurrency-control one.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol


class NonceStore(Protocol):
    def claim(self, key: str, expires_at: int) -> bool: ...
    def seen(self, key: str) -> bool: ...


class InMemoryNonceStore:
    """Single-process replay protection.

    Correct for one process and explicitly not for more: two workers behind a
    load balancer each keep their own set and would each honour the same nonce
    once. A deployment with more than one process needs a shared store with an
    atomic insert — `SETNX`, or a unique index and a caught integrity error.
    That is a deployment decision, so this stays honest about its scope rather
    than hiding behind the Protocol.
    """

    def __init__(self) -> None:
        self._claimed: dict[str, int] = {}
        self._lock = threading.Lock()

    def claim(self, key: str, expires_at: int) -> bool:
        """Record `key` as spent. True only for the first caller."""
        with self._lock:
            self._evict_expired()
            if key in self._claimed:
                return False
            self._claimed[key] = expires_at
            return True

    def seen(self, key: str) -> bool:
        with self._lock:
            return key in self._claimed

    def _evict_expired(self, now: int | None = None) -> None:
        """Drop entries whose authorization can no longer be used anyway.

        Safe precisely because expiry is enforced independently during
        verification: once `validBefore` has passed, the authorization is
        rejected on its own merits, so forgetting the nonce cannot enable a
        replay. Without this the map grows without bound.
        """
        moment = int(time.time()) if now is None else now
        if not self._claimed:
            return
        expired = [k for k, exp in self._claimed.items() if exp <= moment]
        for key in expired:
            del self._claimed[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._claimed)
