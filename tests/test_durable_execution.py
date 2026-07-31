"""A paid build survives the worker that accepted it.

The property under test is the one the buyer cares about: the money moved
on-chain before the build started, so losing the build to a deploy or a crash
means taking payment for nothing. Everything here is about the seam between
"a worker took this job" and "a worker finished this job", and what happens when
the worker disappears in between.

`fakeredis` implements real key expiry and real `LMOVE`/`LREM` semantics, so the
atomicity and the leases are exercised rather than assumed. Where a test needs a
lease to have lapsed it deletes the key: an expired key and a deleted one are
indistinguishable to the code under test, and it saves a sleep. The two tests
that would be vacuous under that shortcut — that a lease is given a TTL at all,
and that a stalled worker really does lose one — use the clock.
"""

from __future__ import annotations

import json
import threading
import time

import fakeredis
import pytest

from src.service.app import build_work, create_app
from src.service.jobs import BuildJob, BuildStatus, InMemoryJobStore, RedisJobStore
from src.service.queue import InMemoryBuildQueue, RedisBuildQueue
from src.service.worker import BuildWorker

from fastapi.testclient import TestClient

from src.payments.x402 import PAYMENT_HEADER, DevPaymentVerifier

PRD_BODY = json.loads(open("examples/todo_app.prd.json", encoding="utf-8").read())
LEASE = "build:queue:lease:"
INFLIGHT = "build:queue:inflight"
PENDING = "build:queue:pending"


@pytest.fixture
def redis_client():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _succeeds(_job):
    def run(job):
        job.status = BuildStatus.SUCCEEDED
        job.log.append("built")
    return run


def _never_runs(_job):
    def run(job):  # pragma: no cover - reaching this is the failure
        raise AssertionError("this build should not have been run")
    return run


# --------------------------------------------------------------------------- #
# The handover: what a worker needs in order to run somebody else's build
# --------------------------------------------------------------------------- #


def test_the_prd_survives_the_process_that_accepted_it(redis_client):
    """The reason durable execution was impossible before.

    The PRD lived in a closure inside the accepting process, so no other worker
    could build the job even in principle — there was nothing to build from.
    """
    job = RedisJobStore(redis_client).create("Round Trip", prd=PRD_BODY)
    job.paid = True
    RedisJobStore(redis_client).save(job)

    # A different process, reading only what is in Redis.
    reloaded = RedisJobStore(redis_client).get(job.id)
    assert reloaded.prd == PRD_BODY
    assert reloaded.paid is True


def test_the_prd_is_not_echoed_back_on_every_poll(redis_client):
    job = RedisJobStore(redis_client).create("Quiet", prd=PRD_BODY)
    assert "prd" not in job.public()
    assert job.public()["attempts"] == 0


def test_the_build_factory_refuses_a_job_no_payment_path_marked_paid():
    """"It was in the queue" is not the authorization. Only the code that
    settled a payment sets `paid`, for the same reason the buyer's own
    `x402_payment_verified` is discarded."""
    unpaid = BuildJob(id="x", app_name="Free Lunch", prd=PRD_BODY, paid=False)
    with pytest.raises(PermissionError):
        build_work(unpaid)


def test_an_unpaid_job_fails_with_a_reason_rather_than_being_built():
    store, queue = InMemoryJobStore(), InMemoryBuildQueue()
    job = store.create("Free Lunch", prd=PRD_BODY)
    queue.push(job.id)

    BuildWorker(store, queue, build_work).run_once()

    assert job.status is BuildStatus.FAILED
    assert "paid" in job.failure


# --------------------------------------------------------------------------- #
# Reserving work
# --------------------------------------------------------------------------- #


def test_a_job_is_reserved_by_exactly_one_worker(redis_client):
    """`LMOVE` is one atomic command. A `RPOP` then `LPUSH` would let two
    workers take the same build, and a crash between the two would lose it."""
    queue = RedisBuildQueue(redis_client)
    queue.push("only-one")

    start = threading.Barrier(8)
    taken: list[str] = []
    lock = threading.Lock()

    def race(n: int) -> None:
        start.wait()
        got = queue.reserve(f"worker-{n}", 60)
        if got is not None:
            with lock:
                taken.append(got)

    threads = [threading.Thread(target=race, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert taken == ["only-one"]


def test_the_queue_is_fifo_so_the_oldest_paid_build_is_not_starved(redis_client):
    queue = RedisBuildQueue(redis_client)
    for job_id in ("first", "second", "third"):
        queue.push(job_id)

    assert [queue.reserve("w", 60) for _ in range(3)] == ["first", "second", "third"]
    assert queue.reserve("w", 60) is None


def test_reserving_gives_the_lease_a_deadline(redis_client):
    """Without a TTL nothing ever expires, so a dead worker's job is held for
    ever and durability is exactly as absent as before."""
    queue = RedisBuildQueue(redis_client)
    queue.push("job")
    queue.reserve("worker-a", 120)

    assert 0 < redis_client.ttl(f"{LEASE}job") <= 120


def test_a_finished_build_is_not_run_again(redis_client):
    """A duplicate queue entry must not overwrite a result the buyer may
    already have downloaded."""
    store, queue = RedisJobStore(redis_client), RedisBuildQueue(redis_client)
    job = store.create("Done")
    job.status = BuildStatus.SUCCEEDED
    store.save(job)
    queue.push(job.id)

    assert BuildWorker(store, queue, _never_runs).run_once() is True
    assert store.get(job.id).status is BuildStatus.SUCCEEDED


def test_acking_leaves_nothing_behind_to_be_run_twice(redis_client):
    store, queue = RedisJobStore(redis_client), RedisBuildQueue(redis_client)
    job = store.create("Once")
    queue.push(job.id)

    BuildWorker(store, queue, _succeeds).run_once()

    assert redis_client.lrange(INFLIGHT, 0, -1) == []
    assert redis_client.lrange(PENDING, 0, -1) == []
    assert redis_client.exists(f"{LEASE}{job.id}") == 0


# --------------------------------------------------------------------------- #
# Losing a worker
# --------------------------------------------------------------------------- #


def _abandon(store, queue, redis_client, job_id, owner="dead-worker"):
    """Leave exactly the state a worker killed mid-build leaves behind.

    It reserved the job, wrote the attempt to the record, and then vanished
    without acking. Its lease stops being renewed and lapses on its own.
    """
    assert queue.reserve(owner, 300) == job_id
    job = store.get(job_id)
    job.attempts += 1
    job.status = BuildStatus.RUNNING
    store.save(job)
    redis_client.delete(f"{LEASE}{job_id}")


def test_a_dead_workers_build_is_finished_by_another_worker(redis_client):
    """The headline. The buyer paid; a different process delivers the build."""
    store, queue = RedisJobStore(redis_client), RedisBuildQueue(redis_client)
    job = store.create("Survivor", prd=PRD_BODY)
    job.paid = True
    store.save(job)
    queue.push(job.id)

    _abandon(store, queue, redis_client, job.id)

    # A worker in another process, which never saw the request that paid for it.
    survivor = BuildWorker(store, queue, _succeeds, recover_every=0)
    assert survivor.run_once() is True

    finished = store.get(job.id)
    assert finished.status is BuildStatus.SUCCEEDED
    assert finished.attempts == 2, "the first worker's attempt is on the record"
    assert any("abandoned" in line for line in finished.log)
    assert redis_client.lrange(INFLIGHT, 0, -1) == []


class _ReadTogether:
    """A Redis client that holds every reader at the inflight scan.

    Simply starting threads together does not produce the race: the whole
    requeue is a handful of fast commands, so the first reaper is usually done
    before the second one looks, and a test built that way passes even with the
    `LREM` guard removed. Blocking inside `lrange` until every reaper has read
    forces the interleaving the guard exists for — all of them holding the same
    snapshot, all of them about to act on it.
    """

    def __init__(self, client, parties: int) -> None:
        self._client = client
        self._barrier = threading.Barrier(parties)

    def __getattr__(self, name):
        return getattr(self._client, name)

    def lrange(self, *args, **kwargs):
        found = self._client.lrange(*args, **kwargs)
        self._barrier.wait(timeout=10)
        return found


def test_an_abandoned_job_is_requeued_once_however_many_workers_notice(redis_client):
    """Every worker reaps, so several can see the same corpse at the same time.
    `LREM`'s return value decides which one requeues it; without that check the
    build is requeued once per reaper and then built that many times."""
    store, queue = RedisJobStore(redis_client), RedisBuildQueue(redis_client)
    job = store.create("Contested")
    queue.push(job.id)
    _abandon(store, queue, redis_client, job.id)

    reapers = 6
    together = _ReadTogether(redis_client, reapers)
    requeued: list[str] = []
    lock = threading.Lock()

    def reap() -> None:
        got = RedisBuildQueue(together).requeue_expired()
        with lock:
            requeued.extend(got)

    threads = [threading.Thread(target=reap) for _ in range(reapers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert requeued == [job.id], "exactly one reaper may claim the requeue"
    assert redis_client.lrange(PENDING, 0, -1) == [job.id]


def test_a_requeued_build_is_not_reaped_while_it_waits_its_turn(redis_client):
    """`reap_stale` fails jobs nothing has touched for half an hour. A requeue
    is something touching it, so the heartbeat has to move — otherwise recovery
    puts a build back in the queue and the next status poll kills it."""
    store, queue = RedisJobStore(redis_client), RedisBuildQueue(redis_client)
    job = store.create("Waiting")
    queue.push(job.id)
    _abandon(store, queue, redis_client, job.id)

    before = store.get(job.id).heartbeat_at
    BuildWorker(store, queue, _succeeds, recover_every=0).recover_abandoned()

    revived = store.get(job.id)
    assert revived.status is BuildStatus.QUEUED
    assert before is None or revived.heartbeat_at > before


def test_a_job_whose_record_is_gone_is_dropped_not_requeued_for_ever(redis_client):
    """Job records expire. An id outliving its record would otherwise be
    reserved, found unbuildable, and requeued on a loop."""
    store, queue = RedisJobStore(redis_client), RedisBuildQueue(redis_client)
    queue.push("vanished")

    assert BuildWorker(store, queue, _never_runs).run_once() is True
    assert redis_client.lrange(INFLIGHT, 0, -1) == []
    assert redis_client.lrange(PENDING, 0, -1) == []


# --------------------------------------------------------------------------- #
# The failure modes durability introduces
# --------------------------------------------------------------------------- #


def test_a_build_that_keeps_killing_workers_is_eventually_given_up_on(redis_client):
    """Requeueing is what makes this dangerous: a build that reliably kills its
    worker would be handed to the next one, and take down the fleet one process
    at a time."""
    store, queue = RedisJobStore(redis_client), RedisBuildQueue(redis_client)
    job = store.create("Poison", prd=PRD_BODY)
    job.paid = True
    job.attempts = 3  # three workers have already taken this and died
    store.save(job)
    queue.push(job.id)

    worker = BuildWorker(store, queue, _never_runs, max_attempts=3)
    assert worker.run_once() is True

    given_up = store.get(job.id)
    assert given_up.status is BuildStatus.FAILED
    assert "exhausted" in given_up.failure
    assert redis_client.lrange(INFLIGHT, 0, -1) == []
    assert redis_client.lrange(PENDING, 0, -1) == [], "not handed to another worker"


def test_a_worker_that_stalls_past_its_lease_discards_its_result(redis_client):
    """Real expiry, real clock. A worker partitioned for longer than its lease
    has already had its job given away; writing its result would clobber
    whatever the new holder is doing."""
    store, queue = RedisJobStore(redis_client), RedisBuildQueue(redis_client)
    job = store.create("Stalled")
    queue.push(job.id)

    started, release = threading.Event(), threading.Event()

    def stalling(_job):
        def run(j):
            started.set()
            release.wait(10)
            j.status = BuildStatus.SUCCEEDED
            j.log.append("finished, but too late to matter")
        return run

    # A heartbeat longer than the lease is the stall: nothing renews in time.
    worker = BuildWorker(
        store, queue, stalling, lease_seconds=1, heartbeat_seconds=30
    )
    thread = threading.Thread(target=worker.run_once)
    thread.start()

    assert started.wait(5)
    time.sleep(1.2)  # the lease lapses while the build is still going
    release.set()
    thread.join(10)

    abandoned = store.get(job.id)
    assert abandoned.status is BuildStatus.RUNNING, "the late result must not land"
    assert not any("too late" in line for line in abandoned.log)


# --------------------------------------------------------------------------- #
# The single-process queue keeps the same contract
# --------------------------------------------------------------------------- #


def test_the_in_memory_queue_honours_the_same_leases():
    queue = InMemoryBuildQueue()
    queue.push("job")

    assert queue.reserve("a", lease_seconds=60) == "job"
    assert queue.renew("job", "a") is True
    assert queue.renew("job", "somebody-else") is False, "a lease has one holder"

    assert queue.requeue_expired(now=time.time() + 3600) == ["job"]
    assert queue.reserve("b", 60) == "job"
    assert queue.renew("job", "a") is False, "the old holder must stand down"


def test_the_in_memory_queue_admits_it_is_not_durable():
    assert InMemoryBuildQueue().durable is False
    assert RedisBuildQueue(fakeredis.FakeStrictRedis(decode_responses=True)).durable


# --------------------------------------------------------------------------- #
# What the operator sees
# --------------------------------------------------------------------------- #


def test_healthz_says_whether_a_paid_build_survives_a_restart(monkeypatch):
    """Both halves have to be durable. A shared queue over an in-memory job
    store loses the record the build would be resumed from."""
    monkeypatch.setenv("X402_SHARED_SECRET", "s")
    app = create_app(
        verifier=DevPaymentVerifier("s"),
        store=InMemoryJobStore(),
        queue=InMemoryBuildQueue(),
        run_worker=False,
    )
    with TestClient(app) as client:
        body = client.get("/healthz").json()

    assert body["durable_execution"] is False
    assert body["queue_depth"] == 0
    assert body["embedded_worker"] is False


def test_an_accepted_build_is_queued_rather_than_run_by_the_web_process(monkeypatch, tmp_path):
    """The accept path's only job is to settle payment and write the work down.
    With no worker running, the build should be sitting in the queue intact."""
    monkeypatch.setattr("src.service.app.BUILD_ROOT", tmp_path)
    # The default generator is `claude`, and a deployment that cannot run it
    # refuses with 503 before anything is queued. This test is about queueing.
    monkeypatch.setenv("SUPERVISOR_GENERATOR", "template")
    store, queue = InMemoryJobStore(), InMemoryBuildQueue()
    app = create_app(
        verifier=DevPaymentVerifier("s"), store=store, queue=queue, run_worker=False
    )

    with TestClient(app) as client:
        response = client.post("/builds", json=PRD_BODY, headers={PAYMENT_HEADER: "s"})

    assert response.status_code == 202
    job = store.get(response.json()["id"])
    assert queue.depth() == 1
    assert job.status is BuildStatus.QUEUED
    assert job.paid is True
    assert job.prd["app_name"] == PRD_BODY["app_name"]
