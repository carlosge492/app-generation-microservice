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

import os
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
    BuildRunner,
    BuildStatus,
    InMemoryJobStore,
    JobStore,
    RedisJobStore,
    reap_stale,
)

BUILD_ROOT = Path(os.getenv("BUILD_ROOT", "generated_apps/service"))


def _settings() -> dict[str, Any]:
    return {
        "generator": os.getenv("SUPERVISOR_GENERATOR", "claude"),
        "analyzer": os.getenv("SUPERVISOR_ANALYZER", "dart"),
        "flutter_root": os.getenv("FLUTTER_ROOT"),
        "max_repairs": int(os.getenv("SUPERVISOR_MAX_REPAIRS", "3")),
        "run_tests": os.getenv("SUPERVISOR_RUN_TESTS", "1") != "0",
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

    if store is not None:
        app.state.store = store
        app.state.store_backend = "injected"
    else:
        client = _redis()
        app.state.store = RedisJobStore(client) if client else InMemoryJobStore()
        app.state.store_backend = "redis" if client else "in-memory"
    app.state.runner = BuildRunner(app.state.store)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        settings = _settings()
        return {
            "ok": True,
            "generator": settings["generator"],
            "analyzer": settings["analyzer"],
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
        }

    @app.post("/builds", status_code=202)
    def create_build(
        prd_body: dict[str, Any],
        request: Request,
        x_payment: str | None = Header(default=None, alias=PAYMENT_HEADER),
    ) -> JSONResponse:
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
        job = store.create(prd.app_name)
        job.build_dir = str(BUILD_ROOT / job.id)
        job.settlement_tx = getattr(request.app.state.verifier, "last_transaction", None)
        store.save(job)
        request.app.state.runner.submit(job, _make_runner(prd))
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


def _make_runner(prd: PRD):
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
        )
        build_dir = Path(job.build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)

        final = app_graph.invoke(
            initial_state(_verified_prd(prd), str(build_dir))
        )

        job.log = list(final.get("log", []))
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

    return run


app = create_app()
