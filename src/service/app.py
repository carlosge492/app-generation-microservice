"""HTTP surface for the app-generation microservice.

    poetry run uvicorn src.service.app:app --port 8000

CLAUDE.md describes an M2M microservice for machine buyers, but everything until
now was a CLI. This is the part a buyer can actually call:

    POST /builds            PRD in, 202 + job id out (402 without payment)
    GET  /builds/{id}       status, log, diagnostics
    GET  /builds/{id}/apk   the artifact
    GET  /healthz

The security decision worth stating plainly: `x402_payment_verified` is a field
on the PRD, and the PRD is supplied by the buyer. That is harmless for a CLI
where the operator writes both, and unacceptable here — a buyer could simply
assert their own payment. The submitted value is therefore discarded and
replaced with whatever the server's verifier concluded from the request. See
`_verified_prd`.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from src.graph.builder import build_graph
from src.graph.state import initial_state
from src.payments.eip3009 import TokenConfig
from src.payments.x402 import (
    PAYMENT_HEADER,
    DevPaymentVerifier,
    PaymentVerifier,
    X402Verifier,
    challenge,
)
from src.ports.analyzer import get_analyzer
from src.ports.generator import get_generator
from src.ports.runtime import FlutterTestRunner
from src.prd.schema import PRD
from src.payments.facilitator import HttpFacilitator
from src.payments.replay import InMemoryNonceStore, RedisNonceStore
from src.service.jobs import (
    BuildJob,
    BuildStatus,
    InMemoryJobStore,
    JobStore,
    RedisJobStore,
    reap_stale,
)
from src.service.queue import (
    DEFAULT_LEASE_SECONDS,
    BuildQueue,
    InMemoryBuildQueue,
    RedisBuildQueue,
)
from src.service.ratelimit import RateLimiter, client_identity
from src.service.artifacts import (
    DEFAULT_RETENTION_SECONDS,
    keep_only_the_artifact,
    sweep_expired,
)
from src.service.worker import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    BuildWorker,
)

log = logging.getLogger(__name__)

BUILD_ROOT = Path(os.getenv("BUILD_ROOT", "generated_apps/service"))

# A finished build is ~2.0 GB of which the buyer wants ~150 MB. Pruning is on by
# default because the alternative is a disk that fills after a few dozen sales
# and fails a build somebody has already paid for; `BUILD_PRUNE=0` keeps the
# whole tree for anyone debugging a deployment.
_PRUNE_BUILDS = os.getenv("BUILD_PRUNE", "1") != "0"
# Matched to RedisJobStore's TTL: once the record expires the download endpoint
# answers 404, so an APK outliving it is unreachable weight.
_RETENTION_SECONDS = int(os.getenv("BUILD_RETENTION_SECONDS", DEFAULT_RETENTION_SECONDS))


def _settings() -> dict[str, Any]:
    return {
        "generator": os.getenv("SUPERVISOR_GENERATOR", "claude"),
        "analyzer": os.getenv("SUPERVISOR_ANALYZER", "dart"),
        "flutter_root": os.getenv("FLUTTER_ROOT"),
        "max_repairs": int(os.getenv("SUPERVISOR_MAX_REPAIRS", "3")),
        "run_tests": os.getenv("SUPERVISOR_RUN_TESTS", "1") != "0",
        # Release emits an unsigned APK the buyer signs themselves; see
        # src/build/signing.py for why the service holds no keys.
        "build_mode": os.getenv("SUPERVISOR_BUILD_MODE", "debug"),
        "sdk_root": os.getenv("ANDROID_SDK_ROOT") or os.getenv("ANDROID_HOME"),
    }


class RefusingVerifier:
    """Used when nothing is configured. Refuses everything.

    A service that cannot verify payment must not accept it. Defaulting to the
    dev shared-secret here would mean a deployment that forgot to configure
    x402 quietly sells builds to anyone who guesses a string.
    """

    last_error = "no payment method configured on this deployment"

    def settle(self, header_value: str | None) -> bool:
        return False


def _redis():
    """A Redis client, or None. Never a silent fallback for the nonce store.

    Falling back to in-memory replay protection when Redis is missing would
    quietly reintroduce the cross-process hole this exists to close, so the
    caller decides what a missing client means — and for nonces it means refuse.
    """
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    import redis  # imported lazily so the CLI never needs it

    return redis.Redis.from_url(url, decode_responses=True)


def _token_from_env() -> TokenConfig | None:
    contract = os.getenv("X402_TOKEN_CONTRACT")
    chain_id = os.getenv("X402_CHAIN_ID")
    if not contract or not chain_id:
        return None
    return TokenConfig(
        chain_id=int(chain_id),
        verifying_contract=contract,
        domain_name=os.getenv("X402_DOMAIN_NAME", "USDC"),
        domain_version=os.getenv("X402_DOMAIN_VERSION", "2"),
        network=os.getenv("X402_NETWORK", "base-sepolia"),
    )


def _verifier_from_env() -> tuple[PaymentVerifier, str, TokenConfig | None]:
    """Pick a verifier, and say plainly which one, for /healthz.

    Order is deliberate: real x402 wins, the dev stand-in is only reachable when
    explicitly configured and no real token is, and the fallback refuses.
    """
    token = _token_from_env()
    pay_to = os.getenv("X402_PAY_TO")
    if token is not None and pay_to:
        price = int(os.getenv("X402_PRICE_ATOMIC", "500000"))
        client = _redis()
        facilitator_url = os.getenv("X402_FACILITATOR_URL")
        return (
            X402Verifier(
                token=token,
                pay_to=pay_to,
                min_value=price,
                clock_skew=int(os.getenv("X402_CLOCK_SKEW", "0")),
                nonces=(
                    RedisNonceStore(client) if client is not None
                    else InMemoryNonceStore()
                ),
                facilitator=(
                    HttpFacilitator(
                        facilitator_url, token, pay_to, price,
                        api_key=os.getenv("X402_FACILITATOR_KEY"),
                        timeout=float(os.getenv("X402_SETTLE_TIMEOUT", "60")),
                    ) if facilitator_url else None
                ),
            ),
            "x402-eip3009",
            token,
        )
    if os.getenv("X402_SHARED_SECRET"):
        return DevPaymentVerifier(os.getenv("X402_SHARED_SECRET")), "dev-shared-secret", None
    return RefusingVerifier(), "none", None


def create_app(
    verifier: PaymentVerifier | None = None,
    store: JobStore | None = None,
    queue: BuildQueue | None = None,
    run_worker: bool | None = None,
) -> FastAPI:
    app = FastAPI(
        title="App-Generation Microservice",
        summary="POST a Product Requirements Document, receive a compiled Flutter APK.",
        version="0.1.0",
    )
    chosen, mode, token = _verifier_from_env()
    app.state.verifier = verifier if verifier is not None else chosen
    app.state.payment_mode = "injected" if verifier is not None else mode
    app.state.token = token
    app.state.pay_to = os.getenv("X402_PAY_TO")
    app.state.price_atomic = int(os.getenv("X402_PRICE_ATOMIC", "500000"))

    # Shares the Redis client below when there is one, so the limit holds across
    # processes rather than per-worker.
    app.state.rate_limiter = RateLimiter(
        limit=int(os.getenv("BUILDS_RATE_LIMIT", "20")),
        window_seconds=int(os.getenv("BUILDS_RATE_WINDOW_SECONDS", "60")),
    )

    client = _redis() if (store is None or queue is None) else None
    if store is not None:
        app.state.store = store
        app.state.store_backend = "injected"
    else:
        app.state.store = RedisJobStore(client) if client else InMemoryJobStore()
        app.state.store_backend = "redis" if client else "in-memory"

    if queue is not None:
        app.state.queue = queue
    else:
        app.state.queue = RedisBuildQueue(client) if client else InMemoryBuildQueue()

    if client is not None:
        app.state.rate_limiter.redis = client

    # An API process builds as well as accepts by default, so a single-container
    # deployment keeps working with no extra moving parts. Set
    # BUILD_WORKER_EMBEDDED=0 on the API and run `python -m src.service.worker`
    # separately once builds and requests want scaling apart.
    embedded = (
        os.getenv("BUILD_WORKER_EMBEDDED", "1") != "0"
        if run_worker is None else run_worker
    )
    app.state.worker = (
        BuildWorker(
            app.state.store, app.state.queue, build_work, **_worker_settings()
        ) if embedded else None
    )
    if app.state.worker is not None:
        app.state.worker.start()

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        settings = _settings()
        return {
            "ok": True,
            "generator": settings["generator"],
            "analyzer": settings["analyzer"],
            # A buyer should be able to tell whether they are getting a debug
            # artifact or an unsigned release one before they pay for it.
            "build_mode": settings["build_mode"],
            # Surfaced because a deployment with no secret refuses every
            # payment, and that should be diagnosable without reading logs.
            "payment_configured": app.state.payment_mode not in {"none"},
            "payment_mode": app.state.payment_mode,
            # Verification proves a signature; settlement proves the money
            # moved. A deployment accepting signed-but-unsettled promises
            # should be able to discover that without reading the source.
            "settlement": (
                "on-chain" if os.getenv("X402_FACILITATOR_URL")
                else "verification-only"
            ),
            "network": app.state.token.network if app.state.token else None,
            "job_store": app.state.store_backend,
            # In-memory anything is single-process. Saying so here is cheaper
            # than someone discovering it from a duplicated build.
            "multi_process_safe": (
                app.state.store_backend == "redis" and bool(os.getenv("REDIS_URL"))
            ),
            # Whether a paid build survives this process dying. Both halves have
            # to be durable: a shared queue over an in-memory job store loses the
            # record the build would be resumed from, which is a misconfiguration
            # worth seeing in one line rather than deducing after an incident.
            "durable_execution": (
                app.state.queue.durable and app.state.store_backend == "redis"
            ),
            # None rather than a 500: an unreadable queue is exactly when an
            # operator is reading /healthz, and losing the other fields to a
            # traceback is the least useful moment to do it.
            "queue_depth": _queue_depth(app.state.queue),
            "embedded_worker": app.state.worker is not None,
            # Surfaced for the same reason as the rest of this payload: an
            # operator should be able to see that the accepting endpoint is
            # bounded without reading the source or the environment.
            "builds_rate_limit": (
                f"{app.state.rate_limiter.limit}/"
                f"{app.state.rate_limiter.window_seconds}s"
                if app.state.rate_limiter.enabled else "unlimited"
            ),
        }

    @app.post("/builds", status_code=202)
    def create_build(
        prd_body: dict[str, Any],
        request: Request,
        x_payment: str | None = Header(default=None, alias=PAYMENT_HEADER),
    ) -> JSONResponse:
        # Before anything expensive. Verifying a payment means two network round
        # trips to the facilitator from a synchronous endpoint, so a flood of
        # junk authorizations exhausts the thread pool and takes the service
        # down for the buyers who did pay.
        limiter = request.app.state.rate_limiter
        retry_after = limiter.check(
            client_identity(
                request.headers.get("x-forwarded-for"),
                request.client.host if request.client else None,
            )
        )
        if retry_after is not None:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "too many requests",
                    "detail": (
                        f"at most {limiter.limit} build requests per "
                        f"{limiter.window_seconds}s from one address"
                    ),
                    "retry_after": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        # Validate before charging: a malformed PRD is the buyer's mistake to
        # fix, and taking payment for work that cannot start would be theft.
        try:
            prd = PRD.model_validate(prd_body)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        verifier = request.app.state.verifier
        if not verifier.settle(x_payment):
            return JSONResponse(
                status_code=402,
                content=challenge(
                    token=request.app.state.token,
                    pay_to=request.app.state.pay_to,
                    max_amount_required=request.app.state.price_atomic,
                    error=getattr(verifier, "last_error", None),
                ),
            )

        store: JobStore = request.app.state.store
        # The PRD is stored on the job rather than captured in a closure, so a
        # worker that never saw this request can still build it. The buyer's own
        # `x402_payment_verified` is stored as sent and overridden at build time
        # by `_verified_prd`, keeping one place that decides it.
        job = store.create(prd.app_name, prd=prd.model_dump(mode="json"))
        job.build_dir = str(BUILD_ROOT / job.id)
        # Reaching here means the verifier settled. Recording that on the job is
        # what lets the worker check it instead of inferring payment from the
        # mere fact that something was queued.
        job.paid = True
        # Explicit, not getattr-with-a-default: the buyer's proof of payment
        # went missing for a while behind exactly that kind of silent fallback.
        verifier = request.app.state.verifier
        job.settlement_tx = (
            verifier.last_transaction
            if isinstance(verifier, X402Verifier) else None
        )
        store.save(job)
        # Saved before queued, never the other way round: a worker can reserve
        # the id the instant it is pushed, and would find no record to build.
        request.app.state.queue.push(job.id)
        return JSONResponse(
            status_code=202,
            content={"id": job.id, "status": job.status.value},
            headers={"Location": f"/builds/{job.id}"},
        )

    @app.get("/builds/{job_id}")
    def get_build(job_id: str, request: Request) -> dict[str, Any]:
        job = request.app.state.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such build")
        # A job whose worker died would otherwise report `running` for ever.
        return reap_stale(request.app.state.store, job).public()

    @app.get("/builds/{job_id}/apk")
    def get_apk(job_id: str, request: Request) -> FileResponse:
        job = request.app.state.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such build")
        if job.status is not BuildStatus.SUCCEEDED:
            # 409, not 404: the build exists, it is simply not finished.
            raise HTTPException(
                status_code=409,
                detail=f"build is {job.status.value}; no artifact yet",
            )
        if not job.apk_path or not Path(job.apk_path).exists():
            raise HTTPException(
                status_code=404,
                detail="build succeeded but produced no APK "
                       "(packaging is skipped unless the toolchain is present)",
            )
        return FileResponse(
            job.apk_path,
            media_type="application/vnd.android.package-archive",
            filename=f"{job.app_name.replace(' ', '_').lower()}.apk",
        )

    return app


def _verified_prd(prd: PRD) -> dict[str, Any]:
    """The PRD the pipeline runs, with payment decided by the server.

    The buyer's own `x402_payment_verified` is discarded rather than trusted.
    Reaching this function at all means the verifier already settled, so it is
    set to True here and nowhere else.
    """
    payload = prd.model_dump(mode="json")
    payload["x402_payment_verified"] = True
    return payload


def build_work(job: BuildJob):
    """What building this job actually means, for a worker that only has the record.

    Handed to `BuildWorker` so that module stays free of LangGraph, PRDs and
    Flutter, and the queue mechanics can be tested without any of them.

    Both checks here are refusals, not assertions: an unpaid or unreadable job
    should fail with a reason the buyer can act on, and the worker turns a raised
    exception into exactly that.
    """
    if not job.paid:
        raise PermissionError(
            "refusing to build a job that no payment path marked as paid"
        )
    if not job.prd:
        raise ValueError("job has no PRD stored; nothing to build")

    prd = PRD.model_validate(job.prd)
    settings = _settings()

    def run(job: BuildJob) -> None:
        app_graph = build_graph(
            get_generator(settings["generator"]),
            get_analyzer(settings["analyzer"], settings["flutter_root"]),
            max_repairs=settings["max_repairs"],
            test_runner=(
                FlutterTestRunner(settings["flutter_root"])
                if settings["run_tests"] else None
            ),
            dry_run=False,
            flutter_root=settings["flutter_root"],
            build_mode=settings["build_mode"],
            sdk_root=settings["sdk_root"],
        )
        build_dir = Path(job.build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)

        final = app_graph.invoke(
            initial_state(_verified_prd(prd), str(build_dir))
        )

        # Appended, not assigned: a requeued build already carries the note
        # saying its first attempt was abandoned, and overwriting that would
        # hide the retry from the only person watching the log.
        job.log = [*job.log, *final.get("log", [])]
        job.diagnostics = [d.render() for d in final.get("diagnostics", [])]

        if final.get("phase") == "failed":
            job.status = BuildStatus.FAILED
            job.failure = final.get("failure") or "build failed"
        elif job.diagnostics:
            job.status = BuildStatus.FAILED
            job.failure = f"{len(job.diagnostics)} unresolved diagnostic(s)"
        else:
            job.status = BuildStatus.SUCCEEDED
            job.apk_path = final.get("apk_path") or None

        # Set here, not left to `_execute` after this closure returns.
        # `InMemoryJobStore` hands out the live `BuildJob` object rather than a
        # copy, so the instant `job.status` above becomes terminal, a concurrent
        # `GET /builds/{id}` can already see it — and pruning below is real
        # filesystem I/O, wide enough for that poll to land in the gap and
        # observe a "succeeded" build with `finished_at` still null. Setting
        # both together, with nothing but attribute assignment between them,
        # closes it. `_execute` only fills this in for the paths that never
        # reach here: an exception, or a build that exits without setting a
        # terminal status at all.
        job.finished_at = datetime.now(timezone.utc)

        # The buyer wants the APK; the other ~2 GB is a Gradle output tree that
        # nothing will ever read again. Pruning here rather than on a timer
        # means the disk is reclaimed while the worker still holds the lease,
        # so no other worker can be reading the directory as it goes.
        if _PRUNE_BUILDS:
            job.apk_path = keep_only_the_artifact(job.build_dir, job.apk_path)
            sweep_expired(BUILD_ROOT, _RETENTION_SECONDS)

    return run


def _queue_depth(queue: BuildQueue) -> int | None:
    try:
        return queue.depth()
    except Exception:  # noqa: BLE001 - /healthz must answer even when Redis is down
        log.exception("could not read the queue depth")
        return None


def _worker_settings() -> dict[str, Any]:
    """Lease timings, which are a deployment decision rather than a constant.

    The lease has to outlast the slowest thing a build does between heartbeats,
    and the heartbeat has to be short enough that a dead worker's build is
    retried while the buyer is still waiting. A deployment generating large apps
    against a cold Gradle cache sits at a different point on that trade than one
    running the template generator, and neither should have to edit the source.
    """
    return {
        "lease_seconds": int(os.getenv("BUILD_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)),
        "heartbeat_seconds": int(
            os.getenv("BUILD_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS)
        ),
        "max_attempts": int(os.getenv("BUILD_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)),
    }


def build_worker_for_environment() -> BuildWorker:
    """The wiring a standalone worker needs, from the same environment the API reads.

    `python -m src.service.worker` calls this. It shares `_redis` and the store
    and queue choices with `create_app` deliberately: a worker that assembled its
    own would eventually disagree with the service about where the work is.
    """
    client = _redis()
    store = RedisJobStore(client) if client else InMemoryJobStore()
    queue = RedisBuildQueue(client) if client else InMemoryBuildQueue()
    return BuildWorker(store, queue, build_work, **_worker_settings())


app = create_app()
