"""Build jobs.

A build runs the LangGraph loop, calls a model several times, shells out to the
Flutter toolchain and can take minutes. That is far too long to hold an HTTP
connection open, so `POST /builds` accepts the work and returns an id.

The store is in-memory and the executor is a thread pool: correct for a single
process, and deliberately not a queue or a database. Anything multi-process
needs both, and pretending otherwise in the type signatures would hide it.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable


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
            "apk_available": bool(self.apk_path and Path(self.apk_path).exists()),
        }


class JobStore:
    def __init__(self, max_workers: int = 2) -> None:
        self._jobs: dict[str, BuildJob] = {}
        self._lock = threading.Lock()
        # Builds are CPU/IO heavy and the graph is synchronous; a small pool
        # keeps a burst of buyers from starting ten Gradle builds at once.
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="build")

    def create(self, app_name: str) -> BuildJob:
        job = BuildJob(id=uuid.uuid4().hex, app_name=app_name)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> BuildJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def submit(self, job: BuildJob, work: Callable[[BuildJob], None]) -> None:
        def run() -> None:
            job.status = BuildStatus.RUNNING
            try:
                work(job)
                # `work` decides SUCCEEDED vs FAILED; anything else is a bug.
                if job.status is BuildStatus.RUNNING:
                    job.status = BuildStatus.FAILED
                    job.failure = "build finished without reporting an outcome"
            except Exception as exc:  # never leave a job wedged in RUNNING
                job.status = BuildStatus.FAILED
                job.failure = f"{type(exc).__name__}: {exc}"
            finally:
                job.finished_at = datetime.now(timezone.utc)

        self._pool.submit(run)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
