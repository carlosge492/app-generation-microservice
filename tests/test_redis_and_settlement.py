"""Shared state and on-chain settlement.

`fakeredis` implements real Redis command semantics — including `SET NX`
atomicity — so the TOCTOU property proved for the in-memory store is proved
again here rather than assumed to have survived the port.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import fakeredis
import httpx
import pytest

from src.payments.eip3009 import TokenConfig
from src.payments.facilitator import (
    HttpFacilitator,
    PrecheckResult,
    SettlementResult,
    payment_requirements,
)
from src.payments.replay import InMemoryNonceStore, RedisNonceStore
from src.service.jobs import (
    BuildJob,
    BuildRunner,
    BuildStatus,
    InMemoryJobStore,
    RedisJobStore,
    reap_stale,
)

TOKEN = TokenConfig(
    chain_id=84532,
    verifying_contract="0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    network="base-sepolia",
)
PAY_TO = "0x000000000000000000000000000000000000dEaD"


@pytest.fixture
def redis_client():
    return fakeredis.FakeStrictRedis(decode_responses=True)


# --------------------------------------------------------------------------- #
# Redis replay protection
# --------------------------------------------------------------------------- #


def test_redis_claim_is_first_caller_wins(redis_client):
    store = RedisNonceStore(redis_client)
    later = int(time.time()) + 600

    assert store.claim("k", later) is True
    assert store.claim("k", later) is False
    assert store.seen("k") is True


def test_redis_claim_is_atomic_under_concurrency(redis_client):
    """The property the whole exercise turns on.

    SETNX is one round trip Redis executes atomically. A `seen()` check followed
    by a separate write would let concurrent callers both pass — which is
    exactly the race the in-memory lock existed to close, and exactly the one a
    naive port would silently reintroduce.
    """
    store = RedisNonceStore(redis_client)
    later = int(time.time()) + 600
    results: list[bool] = []
    barrier = threading.Barrier(16)
    lock = threading.Lock()

    def attempt():
        barrier.wait()
        won = store.claim("contested", later)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=attempt) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 1, f"expected exactly one winner, got {sum(results)}"


def test_two_stores_sharing_redis_see_each_others_claims(redis_client):
    """Stands in for two uvicorn workers. The in-memory store fails this."""
    worker_a = RedisNonceStore(redis_client)
    worker_b = RedisNonceStore(redis_client)
    later = int(time.time()) + 600

    assert worker_a.claim("shared", later) is True
    assert worker_b.claim("shared", later) is False


def test_separate_in_memory_stores_do_not_share_claims():
    """Demonstrates the hole being closed, so the Redis test is not vacuous."""
    later = int(time.time()) + 600
    assert InMemoryNonceStore().claim("shared", later) is True
    assert InMemoryNonceStore().claim("shared", later) is True


def test_claim_ttl_outlives_the_authorization(redis_client):
    """Expiring before validBefore would reopen the replay window."""
    store = RedisNonceStore(redis_client, grace=60)
    expires = int(time.time()) + 300
    store.claim("k", expires)

    ttl = redis_client.ttl("x402:nonce:k")
    assert ttl >= 300, f"ttl {ttl} would let the nonce be replayed while still valid"


def test_already_expired_authorization_is_not_claimed(redis_client):
    store = RedisNonceStore(redis_client)
    assert store.claim("k", int(time.time()) - 10) is False


def test_redis_outage_refuses_payment():
    """Fail closed. Treating an unreachable store as 'never seen' turns an
    infrastructure blip into an unbounded replay window."""

    class DeadRedis:
        def set(self, *a, **k):
            raise ConnectionError("redis is down")

        def exists(self, *a, **k):
            raise ConnectionError("redis is down")

    store = RedisNonceStore(DeadRedis())
    assert store.claim("k", int(time.time()) + 600) is False
    # And an unknown nonce reports as seen, which is the safe direction.
    assert store.seen("k") is True


# --------------------------------------------------------------------------- #
# Redis job store
# --------------------------------------------------------------------------- #


def test_job_survives_a_round_trip_through_redis(redis_client):
    store = RedisJobStore(redis_client)
    job = store.create("Field Notes")
    job.status = BuildStatus.SUCCEEDED
    job.log = ["planning: ok", "qa: 0 errors"]
    job.settlement_tx = "0xabc"
    store.save(job)

    loaded = store.get(job.id)
    assert loaded is not None
    assert loaded.status is BuildStatus.SUCCEEDED
    assert loaded.log == ["planning: ok", "qa: 0 errors"]
    assert loaded.settlement_tx == "0xabc"
    assert loaded.created_at == job.created_at


def test_another_worker_can_read_a_job_it_did_not_create(redis_client):
    accepting_worker = RedisJobStore(redis_client)
    polling_worker = RedisJobStore(redis_client)

    job = accepting_worker.create("Shared")
    assert polling_worker.get(job.id) is not None


def test_unknown_job_is_none(redis_client):
    assert RedisJobStore(redis_client).get("nope") is None


def test_corrupt_job_payload_is_none_not_a_crash(redis_client):
    redis_client.set("build:job:broken", "{not json")
    assert RedisJobStore(redis_client).get("broken") is None


# --------------------------------------------------------------------------- #
# Abandoned builds
# --------------------------------------------------------------------------- #


def test_abandoned_build_is_reaped_rather_than_running_for_ever():
    """Execution is in-process, so a worker restart orphans in-flight builds. A
    status that will never change is worse than a failure: the caller polls it
    indefinitely."""
    store = InMemoryJobStore()
    job = store.create("Orphan")
    job.status = BuildStatus.RUNNING
    job.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=2)
    store.save(job)

    reaped = reap_stale(store, job)
    assert reaped.status is BuildStatus.FAILED
    assert "abandoned" in reaped.failure


def test_a_live_build_is_not_reaped():
    store = InMemoryJobStore()
    job = store.create("Healthy")
    job.status = BuildStatus.RUNNING
    job.heartbeat_at = datetime.now(timezone.utc)
    store.save(job)

    assert reap_stale(store, job).status is BuildStatus.RUNNING


def test_finished_builds_are_left_alone():
    store = InMemoryJobStore()
    job = store.create("Done")
    job.status = BuildStatus.SUCCEEDED
    job.heartbeat_at = datetime.now(timezone.utc) - timedelta(days=1)

    assert reap_stale(store, job).status is BuildStatus.SUCCEEDED


def test_runner_persists_transitions_so_other_workers_see_progress(redis_client):
    store = RedisJobStore(redis_client)
    runner = BuildRunner(store)
    job = store.create("Persisted")

    runner.submit(job, lambda j: setattr(j, "status", BuildStatus.SUCCEEDED))
    deadline = time.time() + 5
    while time.time() < deadline:
        if store.get(job.id).status is BuildStatus.SUCCEEDED:
            break
        time.sleep(0.02)

    assert store.get(job.id).status is BuildStatus.SUCCEEDED
    assert store.get(job.id).finished_at is not None


# --------------------------------------------------------------------------- #
# Settlement
# --------------------------------------------------------------------------- #


def _facilitator(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return HttpFacilitator(
        "https://facilitator.test", TOKEN, PAY_TO, 500_000,
        client=httpx.Client(transport=transport), **kwargs
    )


class _Payment:
    payload = {"x402Version": 1, "scheme": "exact"}


def test_successful_settlement_returns_the_transaction():
    def handler(request):
        assert request.url.path == "/settle"
        return httpx.Response(200, json={"success": True, "transaction": "0xdeadbeef"})

    result = _facilitator(handler).settle(_Payment())
    assert result.ok is True
    assert result.transaction == "0xdeadbeef"


def test_the_request_carries_payload_and_requirements():
    """The facilitator checks the authorization against what we asked for, so it
    needs both — sending only the payload asks it to take our word for it."""
    seen = {}

    def handler(request):
        import json as _json
        seen.update(_json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    _facilitator(handler).settle(_Payment())
    assert seen["paymentPayload"] == _Payment.payload
    assert seen["paymentRequirements"]["payTo"] == PAY_TO
    assert seen["paymentRequirements"]["maxAmountRequired"] == "500000"
    assert seen["paymentRequirements"]["asset"] == TOKEN.verifying_contract


def test_explicit_refusal_is_a_definite_failure_not_unknown():
    def handler(request):
        return httpx.Response(200, json={"success": False, "errorReason": "insufficient_funds"})

    result = _facilitator(handler).settle(_Payment())
    assert result.settled is False
    assert result.unknown is False
    assert "insufficient_funds" in result.reason


def test_a_refusal_is_not_retried():
    """Asking again produces the same answer and only delays the 402."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"success": False, "errorReason": "nope"})

    _facilitator(handler, retries=3).settle(_Payment())
    assert len(calls) == 1


def test_transient_server_error_is_retried_then_reported_unknown():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={"error": "upstream busy"})

    result = _facilitator(handler, retries=2).settle(_Payment())
    assert len(calls) == 3, "a 5xx is the facilitator breaking, not the payment"
    assert result.unknown is True


def test_a_retry_can_succeed_after_a_transient_failure():
    """Retrying is sound because an EIP-3009 nonce is single-use on-chain: a
    duplicate submission reverts rather than charging twice."""
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(502, json={"error": "bad gateway"})
        return httpx.Response(200, json={"success": True, "transaction": "0xok"})

    result = _facilitator(handler, retries=2).settle(_Payment())
    assert result.ok is True
    assert result.transaction == "0xok"


def test_timeout_is_unknown_not_refused():
    """The facilitator may have broadcast and failed to answer. Claiming it
    definitely did not settle would be a lie about whether the buyer was
    charged."""
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    result = _facilitator(handler, retries=1).settle(_Payment())
    assert result.settled is False
    assert result.unknown is True
    assert "timeout" in result.reason


def test_ambiguous_success_field_is_not_treated_as_payment():
    """Settlement is affirmative-only. A body that does not say `success` is not
    a receipt, however encouraging its status code."""
    def handler(request):
        return httpx.Response(200, json={"status": "probably fine"})

    result = _facilitator(handler).settle(_Payment())
    assert result.ok is False
    assert result.unknown is True


def test_unreadable_body_is_unknown():
    def handler(request):
        return httpx.Response(200, content=b"<html>gateway</html>")

    assert _facilitator(handler).settle(_Payment()).unknown is True


def test_client_error_is_a_definite_refusal():
    def handler(request):
        return httpx.Response(400, json={"errorReason": "malformed payload"})

    result = _facilitator(handler).settle(_Payment())
    assert result.unknown is False
    assert "malformed payload" in result.reason


def test_settle_returns_the_receipt_not_just_a_bool():
    """The transaction hash is the buyer's only proof of payment. A bool has
    nowhere to put it, and an earlier version lost it all the way to the API."""
    def handler(request):
        return httpx.Response(200, json={"success": True, "transaction": "0xreceipt"})

    result = _facilitator(handler).settle(_Payment())
    assert result.ok is True
    assert result.transaction == "0xreceipt"


def test_precheck_reads_isvalid_from_the_verify_endpoint():
    """Confirmed against the live PayAI facilitator: /verify answers
    {"isValid": false, "invalidReason": "..."} for an unfunded payer."""
    def handler(request):
        assert request.url.path == "/verify"
        return httpx.Response(200, json={
            "isValid": False,
            "invalidReason": "invalid_exact_evm_insufficient_balance",
            "payer": "0xabc",
        })

    result = _facilitator(handler).precheck(_Payment())
    assert result.valid is False
    assert result.unknown is False
    assert "insufficient_balance" in result.reason


def test_precheck_accepts_a_valid_authorization():
    def handler(request):
        return httpx.Response(200, json={"isValid": True, "payer": "0xabc"})

    assert _facilitator(handler).precheck(_Payment()).valid is True


def test_precheck_failure_is_unknown_when_the_facilitator_is_unreachable():
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    result = _facilitator(handler).precheck(_Payment())
    assert result.valid is False
    assert result.unknown is True


def test_precheck_ambiguity_is_not_treated_as_valid():
    def handler(request):
        return httpx.Response(200, json={"probably": "fine"})

    assert _facilitator(handler).precheck(_Payment()).valid is False


def test_payment_requirements_shape():
    reqs = payment_requirements(TOKEN, PAY_TO, 500_000)
    assert reqs["scheme"] == "exact"
    assert reqs["network"] == "base-sepolia"
    assert reqs["extra"] == {"name": "USDC", "version": "2"}
