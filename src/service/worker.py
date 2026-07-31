"""Pull-based build workers.

    poetry run python -m src.service.worker

A worker reserves a job from the queue, runs it, and keeps its lease alive
throughout so the rest of the fleet can tell it is still there. Kill it and its
lease lapses; another worker requeues the build and runs it from the PRD stored
on the job record. That is the whole of durable execution, and it is why the PRD
had to stop living in a closure inside the accepting process.

This module knows nothing about PRDs, LangGraph or Gradle. It is handed a
`work_factory` that turns a job into the callable that builds it, which keeps
the interesting part — leases, retries, what happens when a build dies halfway —
testable without a Flutter toolchain or a model key. `app.py` supplies what a
build actually is.

Three things a first version gets wrong, so they are stated rather than left to
be rediscovered:

**A build must never be retried for ever.** Requeueing an abandoned job is the
point of all this, but a build that kills its worker — OOM, a Gradle segfault, a
PRD that finds a pathological path through the graph — gets requeued, kills the
next worker, and takes the fleet down one process at a time. `attempts` is
counted on the job, so it survives the worker that incremented it, and a job
past `max_attempts` is failed rather than handed to another victim.

**Losing the lease means standing down, not finishing.** A worker stalled longer
than its lease has already had its job given away. It cannot un-give it, and
writing its results would clobber whatever the new holder is doing, so it
discards them. The build it ran is wasted work; the alternative is two workers
disagreeing about what the buyer bought.

**The heartbeat runs while the build does.** The lease is renewed from a loop
that outlives no single step of the build, because the steps are the slow part:
a model call and a Gradle download can each take minutes, and a lease that only
gets renewed between them would lapse mid-build and hand a perfectly healthy
job to somebody else.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from src.service.jobs import BuildJob, BuildStatus, JobStore
from src.service.queue import (
    DEFAULT_LEASE_SECONDS,
    BuildQueue,
    new_owner_id,
)

log = logging.getLogger(__name__)

# Renewed well inside the lease, so one missed round trip is not a lost job.
DEFAULT_HEARTBEAT_SECONDS = 30

# Two retries after the first attempt. Enough to survive a deploy restarting a
# worker mid-build, few enough that a build which reliably kills workers stops
# being everybody's problem quickly.
DEFAULT_MAX_ATTEMPTS = 3

WorkFactory = Callable[[BuildJob], Callable[[BuildJob], None]]


class BuildWorker:
    """Reserves one job at a time and runs it to a terminal state."""

    def __init__(
        self,
        store: JobStore,
        queue: BuildQueue,
        work_factory: WorkFactory,
        *,
        owner: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        poll_seconds: float = 0.25,
        recover_every: float = 5.0,
    ) -> None:
        self.store = store
        self.queue = queue
        self.work_factory = work_factory
        self.owner = owner or new_owner_id()
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.max_attempts = max_attempts
        self.poll_seconds = poll_seconds
        # An idle worker polls several times a second; scanning for corpses that
        # often is pure Redis traffic, since a lease takes minutes to lapse.
        self.recover_every = recover_every
        self._recovered_at = 0.0
        self._stop = threading.Event()

    # -- one turn of the loop ------------------------------------------------ #

    def run_once(self) -> bool:
        """Recover abandoned jobs, then run at most one. True if it ran one."""
        now = time.monotonic()
        if now - self._recovered_at >= self.recover_every:
            self._recovered_at = now
            self.recover_abandoned()
        job_id = self.queue.reserve(self.owner, self.lease_seconds)
        if job_id is None:
            return False

        job = self.store.get(job_id)
        if job is None:
            # The record expired or was never written. Nothing to build and
            # nothing to report — drop the id rather than requeue it for ever.
            log.warning("queued job %s has no record; dropping it", job_id)
            self.queue.ack(job_id)
            return True

        if job.status in {BuildStatus.SUCCEEDED, BuildStatus.FAILED}:
            # Already finished — a duplicate queue entry, or a job reaped while
            # it sat in the queue. Re-running it would overwrite a result the
            # buyer may already have downloaded.
            self.queue.ack(job_id)
            return True

        job.attempts += 1
        if job.attempts > self.max_attempts:
            self._give_up(job)
            self.queue.ack(job_id)
            return True

        job.status = BuildStatus.RUNNING
        job.heartbeat_at = datetime.now(timezone.utc)
        self.store.save(job)

        if self._execute(job):
            self.queue.ack(job_id)
        return True

    def recover_abandoned(self) -> list[str]:
        """Requeue jobs whose worker stopped renewing, and say so on the record.

        The queue moves the ids; the job records have to be told, or a requeued
        build would sit at `running` with a stale heartbeat and be failed by
        `reap_stale` while it waits its turn.
        """
        requeued = self.queue.requeue_expired()
        for job_id in requeued:
            job = self.store.get(job_id)
            if job is None:
                continue
            job.status = BuildStatus.QUEUED
            job.heartbeat_at = datetime.now(timezone.utc)
            job.log.append(
                f"attempt {job.attempts} was abandoned by its worker; requeued"
            )
            self.store.save(job)
            log.warning("requeued abandoned build %s", job_id)
        return requeued

    # -- running one build --------------------------------------------------- #

    def _execute(self, job: BuildJob) -> bool:
        """Run the build, heartbeating throughout. False if the lease was lost.

        The build runs on its own thread so this one can renew the lease while
        it works. It cannot be cancelled — it is shelling out to Gradle — so
        losing the lease means abandoning the *result*, not the process.
        """
        def run() -> None:
            try:
                # Built inside the thread so that a job the factory refuses —
                # an unreadable PRD, an unpaid record — fails with that reason
                # rather than escaping the loop and being retried three times.
                self.work_factory(job)(job)
                if job.status is BuildStatus.RUNNING:
                    job.status = BuildStatus.FAILED
                    job.failure = "build finished without reporting an outcome"
            except Exception as exc:  # never leave a job wedged in RUNNING
                log.exception("build %s failed", job.id)
                job.status = BuildStatus.FAILED
                job.failure = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=run, name=f"build-{job.id}", daemon=True)
        thread.start()

        while True:
            thread.join(self.heartbeat_seconds)
            if not thread.is_alive():
                break
            if not self.queue.renew(job.id, self.owner, self.lease_seconds):
                log.error(
                    "lost the lease on build %s while running it; another worker "
                    "has taken it. Discarding this attempt's result.", job.id
                )
                return False
            job.heartbeat_at = datetime.now(timezone.utc)
            self.store.save(job)

        # Check once more before writing: the build may have finished during a
        # stall long enough for the job to have been handed to someone else.
        if not self.queue.renew(job.id, self.owner, self.lease_seconds):
            log.error("build %s finished but its lease was gone; result discarded", job.id)
            return False

        # Usually already set: the closure that decides success or failure sets
        # it itself, in the same breath as the terminal status, so that the two
        # become visible together to anyone reading the job mid-flight. This is
        # the backstop for the paths that never reach that closure's own
        # assignment — an exception raised out of it, or a build that returns
        # without setting a terminal status at all.
        if job.finished_at is None:
            job.finished_at = datetime.now(timezone.utc)
        job.heartbeat_at = job.finished_at
        self.store.save(job)
        return True

    def _give_up(self, job: BuildJob) -> None:
        """Fail a build that has exhausted its attempts, permanently."""
        job.status = BuildStatus.FAILED
        job.failure = (
            f"build was abandoned {job.attempts - 1} times and has exhausted its "
            f"{self.max_attempts} attempts. A build that repeatedly kills its "
            "worker is not retried further."
        )
        job.finished_at = datetime.now(timezone.utc)
        job.heartbeat_at = job.finished_at
        self.store.save(job)
        log.error("giving up on build %s after %d attempts", job.id, job.attempts - 1)

    # -- lifecycle ----------------------------------------------------------- #

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                did_work = self.run_once()
            except Exception:  # noqa: BLE001 - a bad job must not kill the loop
                log.exception("worker loop raised; continuing")
                did_work = False
            if not did_work:
                self._stop.wait(self.poll_seconds)

    def start(self) -> threading.Thread:
        """Run the loop on a background thread, for an API process that also builds."""
        thread = threading.Thread(target=self.run_forever, name="build-worker", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()


def main() -> None:
    """A standalone worker: no HTTP surface, just builds.

    Imports `app` here rather than at module scope so the API process can import
    this module without importing itself. The wiring is deliberately the same
    one the API uses — a worker that assembled its own generator or analyzer
    would drift from the service it is supposed to be doing the work for.
    """
    # Importing that module runs `app = create_app()`, which starts an embedded
    # worker unless told otherwise. Without this a standalone worker process
    # would quietly run two of them, at twice the intended concurrency, and
    # BUILD_WORKER_EMBEDDED would control the API process but not this one.
    os.environ["BUILD_WORKER_EMBEDDED"] = "0"

    from src.service.app import build_worker_for_environment

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker = build_worker_for_environment()
    if not worker.queue.durable:
        log.warning(
            "REDIS_URL is not set, so this worker has its own private in-memory "
            "queue and will never see a job the API process accepted."
        )

    # A deploy sends SIGTERM. Finishing the current build would be better still,
    # but the lease already covers the abrupt case: stop taking new work, and
    # let whatever is in flight be requeued if we are killed before it lands.
    signal.signal(signal.SIGTERM, lambda *_: worker.stop())
    log.info("worker %s waiting for builds", worker.owner)
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()


if __name__ == "__main__":
    main()
