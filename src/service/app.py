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

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)

from src.graph.builder import build_graph
from src.graph.state import initial_state
from src.payments.eip3009 import TokenConfig
from src.payments.x402 import (
    PAYMENT_HEADER,
    DevPaymentVerifier,
    PaymentVerifier,
    X402Verifier,
    challenge,
    human_amount,
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
from src.service.mcp import Tool, handle_payload
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


def _generator_unusable() -> str | None:
    """Why this deployment cannot build anything, or None if it can.

    The `claude` generator needs an API key. Without one every build fails on
    the first request to Anthropic — *after* the payment has settled, because
    settlement happens before the job is queued. A deployment in that state
    reports itself healthy, accepts money and delivers nothing, which is the
    worst failure this service has and the one it is least able to notice.

    It happened: `ANTHROPIC_API_KEY=` with a single stray space in .env.deploy
    read as "set" to everything that looked, and a paid build died on
    "Could not resolve authentication method".
    """
    settings = _settings()
    if settings["generator"] != "claude":
        return None
    if not (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        return (
            "the claude generator is selected but ANTHROPIC_API_KEY is empty, so "
            "every build would fail after taking payment"
        )
    return None


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


def _env(name: str, default: str = "") -> str:
    """Read an environment variable, treating empty as unset.

    Compose passes an optional variable as `${NAME:-}`, which puts an *empty
    string* in the container rather than leaving it out. `os.getenv(name,
    default)` falls back only on absence, so the empty string wins and the
    default never applies — `int("")` raises at startup, and an empty EIP-712
    domain name silently rejects every buyer signature. Both are worse than the
    unset case they were meant to cover.
    """
    return os.getenv(name) or default


SERVICE_VERSION = "0.1.0"

# What a buyer receives, advertised in the challenge so an agent can tell whether
# this service is worth paying before it pays. `outputSchema` is an optional v1
# field and the discovery listings that carry one are the ones an agent can use
# without a human first reading prose.
BUILD_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "poll GET /builds/{id}"},
        "status": {"enum": ["queued", "running", "succeeded", "failed"]},
        "apk_available": {
            "type": "boolean",
            "description": "when true, GET /builds/{id}/apk returns the APK",
        },
        "settlement_tx": {"type": "string", "description": "on-chain settlement hash"},
        "log": {"type": "array", "items": {"type": "string"}},
        "diagnostics": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["id", "status"],
}


_LANDING_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PRD to Flutter APK — {price} USDC</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ max-width: 44rem; margin: 3rem auto; padding: 0 1.25rem;
         font: 16px/1.6 system-ui, sans-serif; }}
  code, pre {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .9em; }}
  pre {{ padding: .9rem 1rem; overflow-x: auto; border-radius: 6px;
        background: rgba(127,127,127,.12); }}
  h1 {{ font-size: 1.6rem; margin-bottom: .2rem; }}
  .sub {{ opacity: .7; margin-top: 0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td {{ padding: .35rem .6rem .35rem 0; vertical-align: top; }}
  td:first-child {{ opacity: .7; white-space: nowrap; }}
</style>
<h1>PRD to Flutter APK</h1>
<p class="sub">Send a JSON product requirements document. Get back a compiled
Android APK. <strong>{price} USDC</strong> per build, on {network}.</p>

<p>Payment uses <a href="https://x402.gitbook.io/x402/">x402</a>: POST without
payment and the 402 response carries everything needed to sign an EIP-3009
authorization. The payment settles on-chain before the build starts, and a build
that fails still tells you why — see <code>diagnostics</code> on the job.</p>

<pre>curl -X POST {base}/builds \\
     -H 'Content-Type: application/json' \\
     -d @my-app.prd.json          # 402 with payment terms
# sign, then repeat with:  -H 'X-Payment: &lt;authorization&gt;'</pre>

<p>What happens: the document is planned into a design, a Flutter widget tree is
generated, Riverpod state is wired into it, the result is statically analysed and
repaired until clean, then packaged. Typically 5&ndash;8 minutes.</p>

<p><strong>Using an agent?</strong> Add <code>{base}/mcp</code> as an HTTP MCP
server in Claude or Cursor — nothing to install. It can check your document and
quote the price for free, before anything is paid for.</p>

<table>
  <tr><td>MCP server</td><td><code>{base}/mcp</code></td></tr>
  <tr><td>Free check</td><td><code>POST {base}/validate</code></td></tr>
  <tr><td>Payment terms</td><td><a href="{base}/.well-known/x402">/.well-known/x402</a></td></tr>
  <tr><td>Document schema</td><td><a href="{base}/schema/prd.json">/schema/prd.json</a></td></tr>
  <tr><td>API reference</td><td><a href="{base}/docs">/docs</a></td></tr>
  <tr><td>Service status</td><td><a href="{base}/healthz">/healthz</a></td></tr>
</table>
"""


def _canonical_resource_url() -> str:
    """The service's own public URL for /builds, from configuration.

    Needed because the facilitator is constructed at startup, with no request to
    read a Host header from — and what it sends at settlement becomes this
    service's entry in the public catalog. `PUBLIC_HOSTNAME` is the same value
    the TLS proxy already gets its certificate for, so there is one hostname in
    the deployment rather than two that can disagree.
    """
    host = _env("PUBLIC_HOSTNAME")
    return f"https://{host}/builds" if host else "/builds"


def _public_resource_url(request: Request) -> str:
    """The URL a buyer used, not the one this process sees.

    Behind the TLS proxy the service's own view of itself is `http://api:8000`
    on the compose network, which is unreachable for everyone else — a challenge
    naming it would be unactionable, and `resource` is a field buyers are
    expected to be able to fetch.
    """
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    scheme = forwarded_proto or request.url.scheme
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}{request.url.path}"


def _token_from_env() -> TokenConfig | None:
    contract = _env("X402_TOKEN_CONTRACT")
    chain_id = _env("X402_CHAIN_ID")
    if not contract or not chain_id:
        return None
    return TokenConfig(
        chain_id=int(chain_id),
        verifying_contract=contract,
        domain_name=_env("X402_DOMAIN_NAME", "USDC"),
        domain_version=_env("X402_DOMAIN_VERSION", "2"),
        network=_env("X402_NETWORK", "base-sepolia"),
    )


def _verifier_from_env() -> tuple[PaymentVerifier, str, TokenConfig | None]:
    """Pick a verifier, and say plainly which one, for /healthz.

    Order is deliberate: real x402 wins, the dev stand-in is only reachable when
    explicitly configured and no real token is, and the fallback refuses.
    """
    token = _token_from_env()
    pay_to = _env("X402_PAY_TO")
    if token is not None and pay_to:
        price = int(_env("X402_PRICE_ATOMIC", "500000"))
        client = _redis()
        facilitator_url = _env("X402_FACILITATOR_URL")
        return (
            X402Verifier(
                token=token,
                pay_to=pay_to,
                min_value=price,
                clock_skew=int(_env("X402_CLOCK_SKEW", "0")),
                nonces=(
                    RedisNonceStore(client) if client is not None
                    else InMemoryNonceStore()
                ),
                facilitator=(
                    HttpFacilitator(
                        facilitator_url, token, pay_to, price,
                        api_key=_env("X402_FACILITATOR_KEY") or None,
                        timeout=float(_env("X402_SETTLE_TIMEOUT", "60")),
                        resource=_canonical_resource_url(),
                        output_schema=BUILD_OUTPUT_SCHEMA,
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
        version=SERVICE_VERSION,
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

    @app.get("/llms.txt", include_in_schema=False)
    def llms_txt(request: Request) -> PlainTextResponse:
        """The service explained to a language model, in the llms.txt convention.

        A crawler reading `/` gets marketing prose and a crawler reading
        `/openapi.json` gets 4 KB of schema; neither answers "what is this, what
        does it cost, and how do I call it" in the order an agent needs. Several
        listed x402 services publish one, and directories index it.
        """
        base = _public_resource_url(request).replace("/llms.txt", "")
        price = human_amount(app.state.price_atomic, 6) if app.state.token else "?"
        network = app.state.token.network if app.state.token else "unconfigured"
        return PlainTextResponse(f"""\
# PRD to Flutter APK

> Compiles a JSON product requirements document into an installable Android
> APK. Plans a design, generates the Flutter widget tree, wires Riverpod state
> into it, runs static analysis with an automatic repair loop, then packages the
> build. Typically 5-8 minutes.

Base URL: {base}
Price: {price} USDC per build, on {network}, via x402 (EIP-3009).
Payment settles on-chain before the build starts. No API key, no account.

## MCP

- POST /mcp — remote MCP server (JSON-RPC over HTTP, no install). Tools:
  validate_prd, prd_schema, payment_terms (all free) and build_status.
  Add {base}/mcp as an HTTP MCP server in Claude or Cursor.

## Free

- POST /validate — is this document well-formed, and what would it build?
  Same validator the paid path uses, so anything passing here is accepted there.
- GET /.well-known/x402 — payment terms, machine-readable
- GET /schema/prd.json — JSON Schema for the request body
- GET /builds — answers 402 with the price; costs nothing to ask

## Paid

- POST /builds — body is a PRD (see /schema/prd.json), header X-Payment carries
  a signed EIP-3009 authorization. Returns 202 with a job id.
- GET /builds/{{id}} — status, log, diagnostics, settlement_tx, usage
- GET /builds/{{id}}/apk — the artifact, once apk_available is true

## Notes for agents

A build that fails still returns its diagnostics; payment settles before the
build, so a failure is visible in the job record rather than silent. Poll the
job rather than holding the connection open. The 402 challenge carries the
token contract in `asset` and the EIP-712 domain in `extra`, which is everything
needed to sign without reading this page.
""")

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> PlainTextResponse:
        """Crawlable on purpose. Being found is the point, and the only thing
        worth keeping bots out of is the per-job endpoints, which are unguessable
        ids that cost money to create."""
        return PlainTextResponse(
            "User-agent: *\nAllow: /\nDisallow: /builds/\n"
            f"Sitemap: {_canonical_resource_url().replace('/builds', '/.well-known/x402')}\n"
        )

    @app.get("/", include_in_schema=False)
    def index(request: Request):
        """One page for both audiences.

        A browser gets something readable; anything else gets JSON pointing at
        the manifest. Until now the root answered `{"detail":"Not Found"}` to
        everyone, which is what a human, a crawler and an agent all saw first.
        """
        price = human_amount(app.state.price_atomic, 6) if app.state.token else "?"
        network = app.state.token.network if app.state.token else "unconfigured"
        base = _public_resource_url(request).rstrip("/")

        if "text/html" not in request.headers.get("accept", ""):
            return {
                "name": "PRD to Flutter APK",
                "price_usdc": price,
                "network": network,
                "manifest": f"{base}/.well-known/x402",
                "schema": f"{base}/schema/prd.json",
                "openapi": f"{base}/openapi.json",
                "payment": "x402 (EIP-3009); POST /builds returns 402 with terms",
                "mcp": f"{base}/mcp",
                "free": [f"{base}/validate", f"{base}/llms.txt", f"{base}/schema/prd.json"],
            }

        return HTMLResponse(_LANDING_PAGE.format(price=price, network=network, base=base))

    @app.get("/.well-known/x402")
    def x402_manifest(request: Request) -> dict[str, Any]:
        """What this service sells and what it costs, without buying anything.

        An agent that has to POST a real PRD just to read the price learns the
        terms from a 402 it provoked. The same `accepts` entry is served here,
        in the shape the facilitator's discovery listings use, so a crawler or a
        buyer can decide before it commits to anything.
        """
        resource = _public_resource_url(request).replace("/.well-known/x402", "/builds")
        terms = challenge(
            token=app.state.token,
            pay_to=app.state.pay_to,
            max_amount_required=app.state.price_atomic,
            resource=resource,
            output_schema=BUILD_OUTPUT_SCHEMA,
        )
        return {
            "x402Version": terms["x402Version"],
            "resource": resource,
            "type": "http",
            "method": "POST",
            "accepts": terms["accepts"],
            "name": "PRD to Flutter APK",
            "description": (
                "Compiles a JSON product requirements document into an Android "
                "APK: Flutter widget tree, Riverpod state, static analysis and a "
                "packaged build. Payment settles on-chain before the build starts."
            ),
            "inputSchema": {
                "type": "http",
                "method": "POST",
                "bodySchema": {"$ref": f"{resource.rsplit('/', 1)[0]}/schema/prd.json"},
            },
            "outputSchema": {"type": "json", "schema": BUILD_OUTPUT_SCHEMA},
            "documentation": f"{resource.rsplit('/', 1)[0]}/docs",
        }

    @app.get("/schema/prd.json")
    def prd_schema() -> dict[str, Any]:
        """The PRD contract, as a JSON Schema an agent can generate against.

        Without this an agent has to infer the document shape from 422 errors,
        one missing field at a time — which is how it looked from the outside
        before this existed.
        """
        return PRD.model_json_schema()

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
            # The EIP-712 domain the service verifies signatures against. On
            # the wire this is the difference between accepting a buyer's
            # payment and rejecting every one of them: Base Sepolia's test USDC
            # signs under the name "USDC", Base mainnet's under "USD Coin", and
            # a mismatch fails every signature while looking like a broken
            # deployment rather than a one-word config error.
            "token": (
                {
                    "contract": app.state.token.verifying_contract,
                    "domain_name": app.state.token.domain_name,
                    "domain_version": app.state.token.domain_version,
                    "chain_id": app.state.token.chain_id,
                }
                if app.state.token else None
            ),
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
            # Whether this deployment can actually build. A service that takes
            # payment it cannot fulfil should say so somewhere an operator
            # looks, rather than only in the logs of a build a buyer paid for.
            "generator_ready": _generator_unusable() is None,
            # Surfaced for the same reason as the rest of this payload: an
            # operator should be able to see that the accepting endpoint is
            # bounded without reading the source or the environment.
            "builds_rate_limit": (
                f"{app.state.rate_limiter.limit}/"
                f"{app.state.rate_limiter.window_seconds}s"
                if app.state.rate_limiter.enabled else "unlimited"
            ),
        }

    def _start_build_for_agent(
        prd_body: dict[str, Any], x_payment: str | None, request: Request
    ) -> dict[str, Any]:
        """Buy a build over MCP, by the same route an HTTP buyer takes.

        Delegates to `create_build` rather than reimplementing it: the rate
        limit, the generator-readiness gate, PRD validation and — above all —
        the payment check are one implementation. A second copy here is how an
        MCP path ends up giving builds away that the HTTP path charges for.

        Payment stays the caller's job. An agent holds the wallet and this
        service never sees a key; an unpaid call is answered with the terms to
        sign rather than an error, so a paying agent needs exactly two calls
        and no documentation.
        """
        try:
            answer = create_build(prd_body, request, x_payment)
        except HTTPException as exc:
            # A malformed PRD. Raising through the MCP layer would surface as a
            # bare tool error; the agent can act on the field list.
            return {
                "paid": False,
                "error": "the PRD was rejected before any payment was taken",
                "detail": exc.detail,
                "next": "fix the document and retry; prd_schema has the contract",
            }
        body = json.loads(bytes(answer.body).decode("utf-8"))

        if answer.status_code == 402:
            return {
                "paid": False,
                "next": (
                    "sign an EIP-3009 authorization over accepts[0] and call "
                    "start_build again with it as x_payment"
                ),
                **body,
            }
        if answer.status_code >= 400:
            return {"paid": False, "error": body}

        job_id = body.get("id")
        return {
            "paid": True,
            **body,
            "poll": f"build_status(job_id='{job_id}')",
            "apk_url": f"{_public_resource_url(request).rsplit('/', 1)[0]}/builds/{job_id}/apk",
        }

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        """Remote MCP: paste this URL into Claude or Cursor and the tools appear.

        Every tool below calls the same function the matching HTTP route calls,
        so the MCP surface cannot describe a service the API does not provide.
        """
        payload = await request.json()
        tools = {
            tool.name: tool for tool in (
                Tool(
                    "validate_prd",
                    "Check a product requirements document and report exactly what "
                    "would be built — screens, models, navigation, auth — without "
                    "building it. Free, no payment required. Uses the same validator "
                    "as the paid build, so anything accepted here will not be "
                    "rejected after payment.",
                    {
                        "type": "object",
                        "properties": {"prd": {
                            "type": "object",
                            "description": "the PRD; see the prd_schema tool",
                        }},
                        "required": ["prd"],
                    },
                    lambda prd: validate(prd),
                ),
                Tool(
                    "prd_schema",
                    "The JSON Schema a product requirements document must satisfy. "
                    "Generate against this rather than guessing field names.",
                    {"type": "object", "properties": {}},
                    lambda: PRD.model_json_schema(),
                ),
                Tool(
                    "payment_terms",
                    "What a build costs and how to pay: price, token contract, "
                    "chain and the EIP-712 domain, as an x402 challenge. Free.",
                    {"type": "object", "properties": {}},
                    lambda: challenge(
                        token=app.state.token, pay_to=app.state.pay_to,
                        max_amount_required=app.state.price_atomic,
                        resource=_canonical_resource_url(),
                        output_schema=BUILD_OUTPUT_SCHEMA,
                    ),
                ),
                Tool(
                    "build_status",
                    "Status, log, diagnostics, settlement transaction and token "
                    "usage for a build already paid for.",
                    {
                        "type": "object",
                        "properties": {"job_id": {"type": "string"}},
                        "required": ["job_id"],
                    },
                    lambda job_id: get_build(job_id, request),
                ),
                Tool(
                    "start_build",
                    "Buy a build. Call once without `x_payment` to get the x402 "
                    "payment requirements, sign an EIP-3009 authorization over "
                    "them, then call again passing the signed authorization as "
                    "`x_payment`. Returns a job id; poll it with build_status "
                    "until apk_available is true, then download the APK from "
                    "apk_url. A build takes about 5-8 minutes, so poll rather "
                    "than blocking. Payment settles on-chain before the build "
                    "starts, and a failed build still returns its diagnostics.",
                    {
                        "type": "object",
                        "properties": {
                            "prd": {
                                "type": "object",
                                "description": "the PRD; see the prd_schema tool",
                            },
                            "x_payment": {
                                "type": "string",
                                "description": (
                                    "signed x402 payment authorization (the value "
                                    "of the X-Payment header). Omit to be quoted."
                                ),
                            },
                        },
                        "required": ["prd"],
                    },
                    lambda prd, x_payment=None: _start_build_for_agent(prd, x_payment, request),
                ),
            )
        }

        answer = handle_payload(payload, tools, "prd-to-flutter-apk", SERVICE_VERSION)
        if answer is None:
            # A notification: acknowledged, deliberately with no body.
            return Response(status_code=202)
        return JSONResponse(answer)

    @app.post("/validate")
    def validate(prd_body: dict[str, Any]) -> dict[str, Any]:
        """Free: would this document build, and what would come out?

        Every other route costs $3, which means nobody can evaluate the service
        without first trusting it. This answers the question a buyer actually has
        before paying — is my document well-formed, and is it describing the app
        I think it is — using the same validator the paid path uses, so a
        document that passes here cannot be rejected there.

        Costs nothing to serve: pure schema validation, no model call.
        """
        try:
            prd = PRD.model_validate(prd_body)
        except Exception as exc:
            return {
                "valid": False,
                # The 422 a buyer would otherwise discover one field at a time,
                # after paying.
                "errors": str(exc).splitlines(),
                "schema": "/schema/prd.json",
            }

        navigations = [
            action.target
            for screen in prd.screens for action in screen.actions
            if action.kind == "navigate" and action.target
        ]
        return {
            "valid": True,
            "app_name": prd.app_name,
            "package_name": prd.package_name,
            "theme": prd.theme,
            "would_build": {
                "screens": [
                    {"id": s.id, "title": s.title, "kind": s.kind,
                     "fields": len(s.fields), "actions": len(s.actions)}
                    for s in prd.screens
                ],
                "models": [
                    {"name": m.name, "collection": m.collection, "fields": len(m.fields)}
                    for m in prd.models
                ],
                "firebase_auth": prd.auth,
                "navigation_targets": sorted(set(navigations)),
            },
            "price": {
                "amount": human_amount(app.state.price_atomic, 6),
                "currency": "USDC",
                "network": app.state.token.network if app.state.token else None,
                "buy": "POST the same document to /builds",
            },
        }

    @app.get("/builds")
    def builds_terms(request: Request) -> JSONResponse:
        """An unpaid GET answers 402 with the terms, not 405.

        Building requires a POST with a PRD, so GET has nothing to do — but
        x402 clients, crawlers and directory probers all reach for GET first to
        ask "what does this cost?", and a 405 reads as a broken or non-x402
        endpoint. A discovery directory rejected verification for exactly this,
        which would have left the listing ranked last indefinitely.
        """
        return JSONResponse(
            status_code=402,
            content=challenge(
                token=request.app.state.token,
                pay_to=request.app.state.pay_to,
                max_amount_required=request.app.state.price_atomic,
                resource=_public_resource_url(request),
                output_schema=BUILD_OUTPUT_SCHEMA,
                error="POST a PRD with this header to build; GET only quotes the price",
            ),
        )

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

        # Same principle as validating the PRD below, applied to ourselves: if
        # this deployment cannot build, taking payment for a build is theft
        # whoever's fault the misconfiguration is. 503 rather than 402, because
        # the buyer has done nothing wrong and paying would not help.
        unusable = _generator_unusable()
        if unusable is not None:
            log.error("refusing builds: %s", unusable)
            return JSONResponse(
                status_code=503,
                content={"error": "this deployment cannot build", "detail": unusable},
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
                    # The spec asks for the URL of the protected resource, not
                    # its path. Taken from the request so it is right behind the
                    # TLS proxy, where the service's own view is http://api:8000.
                    resource=_public_resource_url(request),
                    output_schema=BUILD_OUTPUT_SCHEMA,
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
        # Held rather than passed inline: the generator accumulates token usage
        # across every call it makes for this build, and reading it afterwards
        # is the only way to know what the build cost.
        generator = get_generator(settings["generator"])
        app_graph = build_graph(
            generator,
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

        # Recorded whatever the outcome — a failed build still spent tokens,
        # and those are the builds whose cost most needs explaining. Offline
        # generators have no usage to report, hence the getattr.
        usage = getattr(generator, "usage", None)
        if usage is not None:
            job.usage = usage.public()

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
