"""Bounding how often one caller can ask for a build.

The x402 gate stops anyone getting a *free* build, so the thing worth protecting
against is not theft. It is the cost of refusing: `POST /builds` with a payment
header makes the service call the facilitator's `/verify` and then `/settle`,
network round trips with a 60-second timeout, from a synchronous endpoint. A few
hundred concurrent requests carrying junk authorizations exhaust the thread pool
and the service stops answering anyone — including the buyers who did pay.

So the limit is on the accepting endpoint only. Polling a running build and
downloading an APK are cheap and legitimately frequent: a buyer waiting on a
minutes-long build polls every few seconds, and rate-limiting that would punish
exactly the correct behaviour.

**Identifying the caller is the part that goes wrong quietly.** Behind the TLS
proxy every request arrives from Caddy's address on the compose network, so
keying on the socket peer would put every buyer in the world into one bucket —
the limiter would look like it was working while either blocking everyone or
nobody. The client is the *rightmost* entry in `X-Forwarded-For`, because Caddy
appends the peer it actually saw to whatever the client sent. Reading the
leftmost, which is the more common convention, would let a caller send their own
header and mint a fresh identity per request.

Redis-backed when `REDIS_URL` is set, so the limit holds across processes; in
memory otherwise. Unlike the nonce store this *does* fall back rather than
refuse, because a rate limit is a mitigation and not a correctness invariant —
losing it degrades a defence, while losing replay protection would let a
signature buy two builds.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


def client_identity(forwarded_for: str | None, peer: str | None) -> str:
    """Who to charge this request to.

    The rightmost `X-Forwarded-For` entry is the address our own proxy observed;
    everything to its left was supplied by the caller and is therefore a claim
    rather than a fact.
    """
    if forwarded_for:
        hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
        if hops:
            return hops[-1]
    return peer or "unknown"


@dataclass
class _Window:
    count: int = 0
    resets_at: float = 0.0


@dataclass
class RateLimiter:
    """A fixed window per caller. Returns seconds to wait, or None to allow.

    A fixed window rather than a sliding one on purpose: it is one INCR against
    Redis, it needs no stored history per caller, and its worst case — twice the
    limit across a window boundary — is irrelevant at the magnitudes that matter
    here, where the point is stopping hundreds of requests a second.
    """

    limit: int
    window_seconds: int = 60
    redis: object | None = None
    # Namespaces the Redis key. Two limiters guarding different things — paid
    # builds and free previews — must not share a bucket, or the cheap one
    # exhausts the expensive one's allowance.
    name: str = "builds"
    _local: dict[str, _Window] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def check(self, identity: str, now: float | None = None) -> int | None:
        """None if the request may proceed, else seconds until it could."""
        if not self.enabled:
            return None
        now = time.time() if now is None else now
        if self.redis is not None:
            try:
                return self._check_redis(identity, now)
            except Exception:  # noqa: BLE001
                # A Redis outage must not take the API down with it. Payment
                # verification already refuses when Redis is unreachable, which
                # is the failure that matters; degrading to a per-process limit
                # here keeps the service answering.
                log.warning("rate limiter falling back to in-memory", exc_info=True)
        return self._check_local(identity, now)

    def _check_redis(self, identity: str, now: float) -> int | None:
        bucket = int(now // self.window_seconds)
        key = f"ratelimit:{self.name}:{identity}:{bucket}"
        pipe = self.redis.pipeline()
        pipe.incr(key)
        # Set on every request rather than only the first: an INCR that raced
        # with an expiry would otherwise leave a key with no TTL, and that
        # caller would be blocked until the key was evicted.
        pipe.expire(key, self.window_seconds + 1)
        count = pipe.execute()[0]
        if count > self.limit:
            return max(1, int((bucket + 1) * self.window_seconds - now))
        return None

    def _check_local(self, identity: str, now: float) -> int | None:
        with self._lock:
            window = self._local.get(identity)
            if window is None or now >= window.resets_at:
                window = _Window(count=0, resets_at=now + self.window_seconds)
                self._local[identity] = window
            window.count += 1
            if window.count > self.limit:
                return max(1, int(window.resets_at - now))

            # Opportunistic sweep. Without it the dict is an unbounded map of
            # every address that has ever called, which is a slow memory leak
            # on a public endpoint.
            if len(self._local) > 10_000:
                for key in [k for k, v in self._local.items() if now >= v.resets_at]:
                    del self._local[key]
        return None
