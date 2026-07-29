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

import logging
import threading
import time
from typing import Any, Protocol

log = logging.getLogger(__name__)


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


class RedisNonceStore:
    """Replay protection shared across processes.

    `SET key value NX EX ttl` is a single round trip that Redis executes
    atomically, and it returns whether *this* caller created the key. That is
    exactly the property the in-memory lock provided, now holding across
    uvicorn workers and hosts rather than within one interpreter — a
    check-then-set pair of commands would reintroduce the race the lock existed
    to close.

    The TTL is the authorization's own `validBefore`. Expiring earlier would
    reopen the replay window; expiring later would only waste memory, since a
    lapsed authorization is refused on its own merits during verification.

    A Redis outage refuses payment. Returning True when the store is unreachable
    would turn an infrastructure blip into an unbounded replay window, which is
    a far worse failure than rejecting some honest requests.
    """

    def __init__(self, client: Any, prefix: str = "x402:nonce:", grace: int = 60) -> None:
        self._redis = client
        self._prefix = prefix
        # A little past expiry, so clock skew between us and the chain cannot
        # let a still-valid authorization outlive its own claim.
        self._grace = grace

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def claim(self, key: str, expires_at: int) -> bool:
        # Expiry is judged before the grace period is added, or a lapsed
        # authorization would be claimable for another `grace` seconds. The
        # grace exists to make a live claim outlive its authorization, not to
        # extend the authorization itself.
        remaining = int(expires_at - time.time())
        if remaining <= 0:
            # Verification should already have rejected this. Refuse anyway.
            return False
        ttl = remaining + self._grace
        try:
            created = self._redis.set(
                self._key(key), str(int(time.time())), nx=True, ex=ttl
            )
        except Exception:  # noqa: BLE001 - any client error must fail closed
            log.exception("nonce store unavailable; refusing payment")
            return False
        return bool(created)

    def seen(self, key: str) -> bool:
        try:
            return bool(self._redis.exists(self._key(key)))
        except Exception:  # noqa: BLE001
            log.exception("nonce store unavailable")
            # Unknown is reported as seen: the caller uses this for diagnostics,
            # and claiming ignorance of a nonce we cannot check is the unsafe
            # direction to guess in.
            return True
