"""The queue that lets a paid build outlive the worker that accepted it.

Until now `POST /builds` settled the payment and then ran the build on a thread
pool inside the accepting process. Job *state* was shared, so any worker could
report status, but the work itself was not: restart that one process and the
build was gone, with the buyer's money already on-chain. `reap_stale` turned the
loss into an honest failure rather than a job reporting `running` for ever,
which is the least bad thing to do with a build nobody can finish — but it is
still a build nobody can finish.

This is the reliable-queue pattern. A job id is moved from `pending` to
`inflight` in one atomic step, and the worker holding it keeps a lease key alive
while it builds. Two properties follow:

**Nothing is lost between the two lists.** `LMOVE` is a single command, so there
is no moment where an id has left `pending` and not yet reached `inflight`. A
`RPOP` followed by an `LPUSH` would have exactly that gap, and a worker dying
inside it takes the build with it — the failure this module exists to prevent.

**A dead worker is discovered by silence.** The lease expires on its own, so
recovery needs no cooperation from the process that died; that process is, by
assumption, in no position to cooperate. `requeue_expired` moves any inflight
job whose lease has lapsed back to `pending`, and `LREM`'s return value settles
which reaper won — two workers can both notice the same corpse, and exactly one
gets a non-zero reply and does the requeue.

**What a lease does not do.** It bounds duplicate work; it does not make it
impossible. A worker partitioned from Redis for longer than its lease is
indistinguishable from a dead one, so its job is legitimately handed to somebody
else while it is still building. `renew` returns False once that has happened,
which is how the stalled worker learns to stand down, but the two overlap for as
long as the partition lasted. That costs compute and not money: the payment
settled before the job was ever queued, so a build running twice charges once.
Pretending otherwise would need fencing tokens all the way down into Gradle.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from typing import Any, Protocol

log = logging.getLogger(__name__)

# Long enough that an ordinary slow step — a model call, a Gradle download —
# never looks like death, short enough that a genuinely dead worker's build is
# picked up while the buyer is still waiting. The worker renews far more often
# than this, so only a real stall gets anywhere near it.
DEFAULT_LEASE_SECONDS = 300


def new_owner_id() -> str:
    """An identity for one worker process, unique per start.

    Deliberately not the hostname or pid: a restarted worker must not inherit
    the leases of its previous life, or it would renew a lease on a build that
    died with the process before it.
    """
    return uuid.uuid4().hex


class BuildQueue(Protocol):
    durable: bool

    def push(self, job_id: str) -> None: ...
    def reserve(self, owner: str, lease_seconds: int = ...) -> str | None: ...
    def renew(self, job_id: str, owner: str, lease_seconds: int = ...) -> bool: ...
    def ack(self, job_id: str) -> None: ...
    def requeue_expired(self, now: float | None = ...) -> list[str]: ...
    def depth(self) -> int: ...


class InMemoryBuildQueue:
    """A queue for a single-process deployment.

    Durability is not on offer and the attribute says so: the queue dies with
    the process that holds it, so a restart loses whatever was pending. It
    implements the same leases anyway, because a build thread can die without
    its process dying, and because one code path through the worker is worth
    more than the few lines this saves.
    """

    durable = False

    def __init__(self) -> None:
        self._pending: deque[str] = deque()
        self._leases: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def push(self, job_id: str) -> None:
        with self._lock:
            self._pending.append(job_id)

    def reserve(self, owner: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> str | None:
        with self._lock:
            if not self._pending:
                return None
            job_id = self._pending.popleft()
            self._leases[job_id] = (owner, time.time() + lease_seconds)
            return job_id

    def renew(
        self, job_id: str, owner: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> bool:
        with self._lock:
            held = self._leases.get(job_id)
            if held is None or held[0] != owner:
                return False
            self._leases[job_id] = (owner, time.time() + lease_seconds)
            return True

    def ack(self, job_id: str) -> None:
        with self._lock:
            self._leases.pop(job_id, None)

    def requeue_expired(self, now: float | None = None) -> list[str]:
        moment = time.time() if now is None else now
        with self._lock:
            lapsed = [j for j, (_, exp) in self._leases.items() if exp <= moment]
            for job_id in lapsed:
                del self._leases[job_id]
                self._pending.append(job_id)
            return lapsed

    def depth(self) -> int:
        with self._lock:
            return len(self._pending)


class RedisBuildQueue:
    """A queue shared by workers that do not share a process.

    The three keys are a pending list, an inflight list, and one lease key per
    reserved job. Nothing here needs compare-and-set on the job itself: the
    inflight list records *that* a job was taken and the lease records *who* has
    it and *until when*, which is the whole of what recovery needs to know.
    """

    durable = True

    def __init__(
        self,
        client: Any,
        prefix: str = "build:queue:",
        job_ttl: int = 7 * 24 * 3600,
    ) -> None:
        self._redis = client
        self._pending = f"{prefix}pending"
        self._inflight = f"{prefix}inflight"
        self._lease_prefix = f"{prefix}lease:"
        # Matches the job store's retention. A lease key outliving every trace
        # of its job would pin an id in `inflight` that can never be requeued.
        self._job_ttl = job_ttl

    def _lease_key(self, job_id: str) -> str:
        return f"{self._lease_prefix}{job_id}"

    def push(self, job_id: str) -> None:
        self._redis.lpush(self._pending, job_id)

    def reserve(self, owner: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> str | None:
        # RIGHT/LEFT with LPUSH above makes this FIFO: the oldest paid build is
        # the one waiting longest, and it should not be starved by newer ones.
        job_id = self._redis.lmove(self._pending, self._inflight, "RIGHT", "LEFT")
        if job_id is None:
            return None
        # A crash between the move and this write leaves an inflight job with no
        # lease, which `requeue_expired` reads as expired and puts back. The
        # unsafe ordering would be the reverse — lease first, then move — where
        # the same crash strands the id in `pending` with a live lease on it.
        self._redis.set(self._lease_key(job_id), owner, ex=lease_seconds)
        return job_id

    def renew(
        self, job_id: str, owner: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> bool:
        """Extend our lease, and report whether it was still ours to extend.

        Read-then-write rather than one atomic compare-and-expire, because the
        atomic form is a Lua script and this store is proved against `fakeredis`,
        which has no Lua. The check that matters — has somebody else taken this
        job? — is exact: a takeover writes a different owner and the read sees
        it. The residual race is our EXPIRE landing microseconds after the lease
        we just read lapsed and was re-taken, which extends the *new* holder's
        lease rather than resurrecting ours. Harmless, and strictly rarer than
        the partition case this exists to catch.
        """
        try:
            held = self._redis.get(self._lease_key(job_id))
        except Exception:  # noqa: BLE001 - an unreachable store is a lost lease
            log.exception("could not read lease for %s", job_id)
            return False
        if held != owner:
            return False
        return bool(self._redis.expire(self._lease_key(job_id), lease_seconds))

    def ack(self, job_id: str) -> None:
        """Finished with this job — drop it from inflight and release the lease."""
        self._redis.lrem(self._inflight, 0, job_id)
        self._redis.delete(self._lease_key(job_id))

    def requeue_expired(self, now: float | None = None) -> list[str]:
        """Return abandoned jobs to `pending`. Safe to run from every worker.

        `LREM` reports how many entries it removed, and that is what decides the
        race: several workers may see the same expired lease, but only the one
        whose LREM returns non-zero pushes the id back. Without that check the
        job would be requeued once per reaper and built that many times.
        """
        requeued: list[str] = []
        for job_id in self._redis.lrange(self._inflight, 0, -1):
            if self._redis.exists(self._lease_key(job_id)):
                continue
            if self._redis.lrem(self._inflight, 1, job_id):
                self._redis.lpush(self._pending, job_id)
                requeued.append(job_id)
        return requeued

    def depth(self) -> int:
        return int(self._redis.llen(self._pending))
