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
from src.payments.x402 import (
    PAYMENT_HEADER,
    DevPaymentVerifier,
    PaymentVerifier,
    challenge,
)
from src.ports.analyzer import get_analyzer
from src.ports.generator import get_generator
from src.ports.runtime import FlutterTestRunner
from src.prd.schema import PRD
from src.service.jobs import BuildJob, BuildStatus, JobStore

BUILD_ROOT = Path(os.getenv("BUILD_ROOT", "generated_apps/service"))


def _settings() -> dict[str, Any]:
    return {
        "generator": os.getenv("SUPERVISOR_GENERATOR", "claude"),
        "analyzer": os.getenv("SUPERVISOR_ANALYZER", "dart"),
        "flutter_root": os.getenv("FLUTTER_ROOT"),
        "max_repairs": int(os.getenv("SUPERVISOR_MAX_REPAIRS", "3")),
        "run_tests": os.getenv("SUPERVISOR_RUN_TESTS", "1") != "0",
    }


def create_app(
    verifier: PaymentVerifier | None = None,
    store: JobStore | None = None,
) -> FastAPI:
    app = FastAPI(
        title="App-Generation Microservice",
        summary="POST a Product Requirements Document, receive a compiled Flutter APK.",
        version="0.1.0",
    )
    app.state.verifier = verifier or DevPaymentVerifier(os.getenv("X402_SHARED_SECRET"))
    app.state.store = store or JobStore()

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        settings = _settings()
        return {
            "ok": True,
            "generator": settings["generator"],
            "analyzer": settings["analyzer"],
            # Surfaced because a deployment with no secret refuses every
            # payment, and that should be diagnosable without reading logs.
            "payment_configured": bool(os.getenv("X402_SHARED_SECRET")),
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

        paid = request.app.state.verifier.settle(x_payment)
        if not paid:
            return JSONResponse(status_code=402, content=challenge())

        store: JobStore = request.app.state.store
        job = store.create(prd.app_name)
        job.build_dir = str(BUILD_ROOT / job.id)
        store.submit(job, _make_runner(prd))
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
        return job.public()

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
