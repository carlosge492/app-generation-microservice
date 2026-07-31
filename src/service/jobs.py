"""Build jobs: the record of one paid build.

A build runs the graph, calls a model several times and shells out to Gradle, so
it takes minutes — far too long to hold an HTTP connection open. `POST /builds`
accepts the work and returns an id.

Storage and execution are separate concerns and are separated here: this module
owns what a job *is* and where it is kept, `queue.py` owns who gets to run it,
and `worker.py` owns running it. The job record is the handover point, which is
why it carries the PRD. Holding the PRD in a closure — as this did while
execution lived in the accepting process — meant the work existed only in the
memory of one process, so no other worker could pick up a build even in
principle. Durable execution starts with writing down what was bought.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)


class BuildStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class BuildJob:
    id: str
    app_name: str
    status: BuildStatus = BuildStatus.QUEUED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    log: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    failure: str | None = None
    apk_path: str | None = None
    build_dir: str | None = None
    heartbeat_at: datetime | None = None
    settlement_tx: str | None = None
    # Token counts and an estimated cost for this build, or empty for the
    # offline generators, which spend nothing. This is the only record of what
    # a sale cost to fulfil — without it the price is a guess.
    usage: dict[str, Any] = field(default_factory=dict)
    # The work itself, stored rather than closed over, so a worker that did not
    # accept this build can still run it. See the module docstring.
    prd: dict[str, Any] | None = None
    # Set only by the path that settled a payment. The worker checks it before
    # building, so "it was in the queue" is never by itself the authorization —
    # the same reason the buyer's own `x402_payment_verified` is discarded.
    paid: bool = False
    # How many times a worker has picked this up. Bounded in `worker.py`: a
    # build that kills its worker would otherwise be retried for ever.
    attempts: int = 0

    def public(self) -> dict[str, Any]:
        """What the buyer sees. `prd` is deliberately absent — they sent it, and
        echoing it back on every poll of a minutes-long build is pure weight."""
        return {
            "id": self.id,
            "app_name": self.app_name,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "log": self.log,
            "diagnostics": self.diagnostics,
            "failure": self.failure,
            "settlement_tx": self.settlement_tx,
            # A buyer polling a build that quietly restarted deserves to see
            # that it did, rather than wondering why the log went backwards.
            "attempts": self.attempts,
            "apk_available": bool(self.apk_path and Path(self.apk_path).exists()),
            # What this build cost to produce. Shown to the buyer as well as
            # the operator: an M2M buyer deciding whether the price is fair has
            # the same interest in it that the seller does.
            "usage": self.usage,
        }

    # -- serialisation for a shared store ----------------------------------- #

    def to_json(self) -> str:
        raw = asdict(self)
        raw["status"] = self.status.value
        for stamp in ("created_at", "finished_at", "heartbeat_at"):
            value = getattr(self, stamp)
            raw[stamp] = value.isoformat() if value else None
        return json.dumps(raw)

    @classmethod
    def from_json(cls, blob: str | bytes) -> BuildJob:
        raw = json.loads(blob)
        raw["status"] = BuildStatus(raw["status"])
        for stamp in ("created_at", "finished_at", "heartbeat_at"):
            raw[stamp] = datetime.fromisoformat(raw[stamp]) if raw.get(stamp) else None
        return cls(**raw)


class JobStore(Protocol):
    def create(self, app_name: str, prd: dict[str, Any] | None = ...) -> BuildJob: ...
    def get(self, job_id: str) -> BuildJob | None: ...
    def save(self, job: BuildJob) -> None: ...


class InMemoryJobStore:
    """Single-process job state. Fine for a CLI-shaped deployment.

    Note what this cannot do however durable the queue is: the records live in
    one process, so a restart loses them even if the queue kept the ids. Pairing
    a durable queue with this store is a misconfiguration, and `/healthz`
    reports durability from both.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, BuildJob] = {}
        self._lock = threading.Lock()

    def create(self, app_name: str, prd: dict[str, Any] | None = None) -> BuildJob:
        job = BuildJob(id=uuid.uuid4().hex, app_name=app_name, prd=prd)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> BuildJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def save(self, job: BuildJob) -> None:
        with self._lock:
            self._jobs[job.id] = job


class RedisJobStore:
    """Job state shared across workers.

    Unlike the nonce store, this one does not need compare-and-set: a job is
    created once by the worker that accepted it and, at any moment, written only
    by whichever worker holds its lease, so last-write-wins is correct here. The
    nonce store's `SETNX` matters because two callers race for the same key; the
    queue's leases keep anything from racing for a job id.

    Jobs expire, because a build log is not a permanent record and an unbounded
    key space is an outage waiting to happen. The queue uses the same retention,
    so a job record and its place in the queue disappear together.
    """

    def __init__(self, client: Any, prefix: str = "build:job:", ttl: int = 7 * 24 * 3600):
        self._redis = client
        self._prefix = prefix
        self._ttl = ttl

    def _key(self, job_id: str) -> str:
        return f"{self._prefix}{job_id}"

    def create(self, app_name: str, prd: dict[str, Any] | None = None) -> BuildJob:
        job = BuildJob(id=uuid.uuid4().hex, app_name=app_name, prd=prd)
        self.save(job)
        return job

    def get(self, job_id: str) -> BuildJob | None:
        blob = self._redis.get(self._key(job_id))
        if blob is None:
            return None
        try:
            return BuildJob.from_json(blob)
        except (ValueError, TypeError, KeyError):
            log.exception("job %s is unreadable", job_id)
            return None

    def save(self, job: BuildJob) -> None:
        self._redis.set(self._key(job.id), job.to_json(), ex=self._ttl)


STALE_AFTER = timedelta(minutes=30)


def reap_stale(store: JobStore, job: BuildJob, now: datetime | None = None) -> BuildJob:
    """Fail a job that no worker is going to finish.

    The queue's leases recover a build whose worker died: it goes back to
    `pending` and somebody picks it up. That recovery needs the queue to have
    outlived the worker, which is true of the Redis queue and false of the
    in-memory one — kill that process and its pending list dies with it, while
    the job records in a shared store live on, describing builds that will never
    run again.

    This is the backstop for exactly that case, and for a job whose id was lost
    from the queue by any other means. A status that will never change is worse
    than a failure, because a caller polls it indefinitely.

    The threshold is measured from the last heartbeat, and a requeue refreshes
    it, so a build waiting its turn behind a long queue is not mistaken for an
    abandoned one.
    """
    if job.status not in {BuildStatus.RUNNING, BuildStatus.QUEUED}:
        return job
    moment = now or datetime.now(timezone.utc)
    last_seen = job.heartbeat_at or job.created_at
    if moment - last_seen <= STALE_AFTER:
        return job

    job.status = BuildStatus.FAILED
    job.failure = (
        "build was abandoned — no worker has reported on it for "
        f"{int(STALE_AFTER.total_seconds() // 60)} minutes, and none is "
        "expected to. A durable queue (REDIS_URL) lets an abandoned build be "
        "retried by another worker instead."
    )
    job.finished_at = moment
    store.save(job)
    return job
