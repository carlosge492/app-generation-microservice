"""Build jobs: shared state, in-process execution.

A build runs the graph, calls a model several times and shells out to Gradle, so
it takes minutes — far too long to hold an HTTP connection open. `POST /builds`
accepts the work and returns an id.

Storage and execution are separate concerns and are separated here. Job *state*
lives in a store that can be shared across processes, so any uvicorn worker can
answer `GET /builds/{id}` for a build another worker is running. Job *execution*
is still a thread pool inside the accepting worker.

That asymmetry is worth stating rather than discovering: a shared store makes
status readable everywhere, but the work itself is not distributed and not
durable. Restart the worker running a build and that build is lost — its state
will sit at `running` until it is reaped. Real durability needs a queue and
workers that pull from it, which is a deployment change rather than a code
tweak, so `reap_stale` exists to stop abandoned jobs lying about their status
instead of pretending the problem is solved.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

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

    def public(self) -> dict[str, Any]:
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
            "apk_available": bool(self.apk_path and Path(self.apk_path).exists()),
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
    def create(self, app_name: str) -> BuildJob: ...
    def get(self, job_id: str) -> BuildJob | None: ...
    def save(self, job: BuildJob) -> None: ...


class InMemoryJobStore:
    """Single-process job state. Fine for a CLI-shaped deployment."""

    def __init__(self) -> None:
        self._jobs: dict[str, BuildJob] = {}
        self._lock = threading.Lock()

    def create(self, app_name: str) -> BuildJob:
        job = BuildJob(id=uuid.uuid4().hex, app_name=app_name)
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
    created once by the worker that accepted it and only ever written by the
    worker running it, so last-write-wins is correct here. The nonce store's
    `SETNX` matters because two callers race for the same key; nothing races for
    a job id.

    Jobs expire, because a build log is not a permanent record and an unbounded
    key space is an outage waiting to happen.
    """

    def __init__(self, client: Any, prefix: str = "build:job:", ttl: int = 7 * 24 * 3600):
        self._redis = client
        self._prefix = prefix
        self._ttl = ttl

    def _key(self, job_id: str) -> str:
        return f"{self._prefix}{job_id}"

    def create(self, app_name: str) -> BuildJob:
        job = BuildJob(id=uuid.uuid4().hex, app_name=app_name)
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
    """Fail a job whose worker died mid-build.

    Execution is in-process, so a restart abandons whatever that worker was
    running and its state stays `running` for ever. A status that will never
    change is worse than a failure, because a caller polls it indefinitely.
    """
    if job.status not in {BuildStatus.RUNNING, BuildStatus.QUEUED}:
        return job
    moment = now or datetime.now(timezone.utc)
    last_seen = job.heartbeat_at or job.created_at
    if moment - last_seen <= STALE_AFTER:
        return job

    job.status = BuildStatus.FAILED
    job.failure = (
        "build was abandoned — the worker running it stopped reporting. "
        "Execution is in-process, so a restart loses in-flight builds."
    )
    job.finished_at = moment
    store.save(job)
    return job


class BuildRunner:
    """Runs builds on a bounded thread pool, persisting every transition."""

    def __init__(self, store: JobStore, max_workers: int = 2) -> None:
        self.store = store
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="build"
        )

    def submit(self, job: BuildJob, work: Callable[[BuildJob], None]) -> None:
        def run() -> None:
            job.status = BuildStatus.RUNNING
            job.heartbeat_at = datetime.now(timezone.utc)
            self.store.save(job)
            try:
                work(job)
                if job.status is BuildStatus.RUNNING:
                    job.status = BuildStatus.FAILED
                    job.failure = "build finished without reporting an outcome"
            except Exception as exc:  # never leave a job wedged in RUNNING
                job.status = BuildStatus.FAILED
                job.failure = f"{type(exc).__name__}: {exc}"
            finally:
                job.finished_at = datetime.now(timezone.utc)
                job.heartbeat_at = job.finished_at
                self.store.save(job)

        self._pool.submit(run)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
