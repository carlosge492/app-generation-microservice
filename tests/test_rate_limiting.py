"""Bounding the accepting endpoint.

The x402 gate already stops anyone getting a free build, so this is not about
theft. `POST /builds` with a payment header makes two network round trips to the
facilitator, with a 60-second timeout, from a synchronous endpoint — so a flood
of junk authorizations exhausts the thread pool and the service stops answering
the buyers who did pay.

The identity half is the part that fails quietly rather than loudly, and most of
these tests are about that: behind the TLS proxy every request arrives from
Caddy, so a limiter keyed on the socket peer would put the entire internet in one
bucket and either block everyone or nobody, while looking configured either way.
"""

from __future__ import annotations

import json

import fakeredis
import pytest
from fastapi.testclient import TestClient

from src.service.app import create_app
from src.service.ratelimit import RateLimiter, client_identity

PRD_BODY = json.load(open("examples/todo_app.prd.json", encoding="utf-8"))


# -- who is the caller? ------------------------------------------------------ #

def test_the_client_is_the_rightmost_forwarded_hop():
    """Caddy appends the peer it actually saw to whatever the client sent, so
    the rightmost entry is the only one we observed rather than were told."""
    assert client_identity("203.0.113.9", "172.18.0.5") == "203.0.113.9"


def test_a_spoofed_forwarded_header_cannot_mint_identities():
    """Reading the leftmost entry — the more common convention — would let a
    caller send their own X-Forwarded-For and get a fresh bucket per request,
    which is the whole limit defeated by one header."""
    forged = "1.1.1.1, 2.2.2.2, 203.0.113.9"

    assert client_identity(forged, "172.18.0.5") == "203.0.113.9"


def test_without_a_proxy_the_socket_peer_is_used():
    assert client_identity(None, "198.51.100.4") == "198.51.100.4"
    assert client_identity("", "198.51.100.4") == "198.51.100.4"


def test_an_unidentifiable_caller_still_gets_a_bucket():
    """Sharing one bucket is the safe direction: unknown callers throttle each
    other rather than bypassing the limit entirely."""
    assert client_identity(None, None) == "unknown"


# -- the limiter itself ------------------------------------------------------ #

@pytest.fixture(params=["memory", "redis"])
def limiter(request):
    """Both backends, because the deployment uses Redis and the tests would
    otherwise only ever exercise the fallback."""
    if request.param == "redis":
        return RateLimiter(limit=3, window_seconds=60, redis=fakeredis.FakeRedis())
    return RateLimiter(limit=3, window_seconds=60)


def test_requests_up_to_the_limit_are_allowed(limiter):
    assert [limiter.check("a", now=1000.0) for _ in range(3)] == [None, None, None]


def test_the_next_request_is_refused_with_a_wait(limiter):
    for _ in range(3):
        limiter.check("a", now=1000.0)

    retry_after = limiter.check("a", now=1000.0)

    assert retry_after is not None and retry_after > 0


def test_callers_do_not_share_a_bucket(limiter):
    for _ in range(4):
        limiter.check("noisy", now=1000.0)

    assert limiter.check("quiet", now=1000.0) is None


def test_the_window_rolls_over(limiter):
    for _ in range(4):
        limiter.check("a", now=1000.0)

    assert limiter.check("a", now=1000.0) is not None
    assert limiter.check("a", now=1121.0) is None


def test_a_zero_limit_disables_it(limiter):
    limiter.limit = 0

    assert limiter.enabled is False
    assert all(limiter.check("a", now=1000.0) is None for _ in range(50))


def test_a_redis_outage_degrades_instead_of_failing():
    """A rate limit is a mitigation, not a correctness invariant. Payment
    verification already refuses when Redis is unreachable — the failure that
    actually matters — so this keeps answering on a per-process limit."""
    class Broken:
        def pipeline(self):
            raise ConnectionError("redis is gone")

    limiter = RateLimiter(limit=2, window_seconds=60, redis=Broken())

    assert limiter.check("a", now=1000.0) is None
    assert limiter.check("a", now=1000.0) is None
    assert limiter.check("a", now=1000.0) is not None


def test_redis_backed_limits_are_shared_between_processes():
    """Two API containers behind one proxy must not each grant the full limit."""
    shared = fakeredis.FakeRedis()
    one = RateLimiter(limit=2, window_seconds=60, redis=shared)
    two = RateLimiter(limit=2, window_seconds=60, redis=shared)

    assert one.check("a", now=1000.0) is None
    assert two.check("a", now=1000.0) is None
    assert two.check("a", now=1000.0) is not None


# -- through the endpoint ---------------------------------------------------- #

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BUILDS_RATE_LIMIT", "2")
    monkeypatch.setenv("BUILDS_RATE_WINDOW_SECONDS", "60")
    monkeypatch.setenv("BUILD_WORKER_EMBEDDED", "0")
    return TestClient(create_app())


def test_the_endpoint_refuses_past_the_limit_with_retry_after(client):
    headers = {"X-Forwarded-For": "203.0.113.9"}
    seen = [
        client.post("/builds", json=PRD_BODY, headers=headers).status_code
        for _ in range(3)
    ]

    assert seen[:2] == [402, 402], "unpaid requests are refused for payment first"
    assert seen[2] == 429

    refused = client.post("/builds", json=PRD_BODY, headers=headers)
    assert refused.headers["Retry-After"].isdigit()
    assert "retry_after" in refused.json()


def test_the_limit_is_applied_before_anything_expensive(client):
    """Order matters: verifying a payment costs two facilitator round trips, so
    a limiter running after it would be protecting nothing.

    A malformed body is the probe. Under the limit it is rejected by validation
    with 422 — which is what makes the 429 afterwards evidence of ordering
    rather than a coincidence about which handler happens to answer."""
    fresh = {"X-Forwarded-For": "198.51.100.77"}
    assert client.post("/builds", json={"nonsense": True}, headers=fresh).status_code == 422

    for _ in range(3):
        client.post("/builds", json=PRD_BODY, headers=fresh)

    assert client.post(
        "/builds", json={"nonsense": True}, headers=fresh
    ).status_code == 429


def test_polling_and_downloading_are_not_limited(client):
    """A buyer waiting on a minutes-long build polls every few seconds. Limiting
    that would punish exactly the correct behaviour."""
    headers = {"X-Forwarded-For": "203.0.113.9"}
    for _ in range(5):
        client.post("/builds", json=PRD_BODY, headers=headers)

    for _ in range(20):
        assert client.get("/builds/does-not-exist", headers=headers).status_code == 404


def test_different_buyers_are_not_throttled_by_a_noisy_one(client):
    noisy = {"X-Forwarded-For": "203.0.113.9"}
    for _ in range(5):
        client.post("/builds", json=PRD_BODY, headers=noisy)

    quiet = client.post(
        "/builds", json=PRD_BODY, headers={"X-Forwarded-For": "198.51.100.4"}
    )

    assert quiet.status_code == 402, "refused for payment, not for rate"


def test_healthz_reports_the_limit(client):
    assert client.get("/healthz").json()["builds_rate_limit"] == "2/60s"
