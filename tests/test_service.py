"""HTTP surface tests.

No model calls and no toolchain: the generator is the offline template one and
the payment verifier is injected, so these run anywhere.
"""

from __future__ import annotations

import json
import re
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from src.payments.x402 import PAYMENT_HEADER, DevPaymentVerifier
from src.service.app import _verified_prd, build_work, create_app
from src.service.jobs import BuildStatus, InMemoryJobStore
from src.service.queue import InMemoryBuildQueue
from src.service.worker import BuildWorker
from src.prd.schema import load_prd

SECRET = "test-secret"
PRD_BODY = json.loads(open("examples/todo_app.prd.json", encoding="utf-8").read())


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Offline everything: no API key, no Flutter, no packaging.
    monkeypatch.setenv("SUPERVISOR_GENERATOR", "template")
    monkeypatch.setenv("SUPERVISOR_ANALYZER", "stub")
    monkeypatch.setenv("SUPERVISOR_RUN_TESTS", "0")
    monkeypatch.setattr("src.service.app.BUILD_ROOT", tmp_path)
    return TestClient(create_app(verifier=DevPaymentVerifier(SECRET), store=InMemoryJobStore()))


def test_a_deployment_that_cannot_build_refuses_before_charging(monkeypatch):
    """The service must not take money for a build it cannot possibly run.

    `SUPERVISOR_GENERATOR=claude` with no API key fails on the first request to
    Anthropic — after settlement, because payment settles before the job is
    queued. It happened on the live deployment: a stray space in .env.deploy
    read as a key to everything that looked, and a paid build died on "Could not
    resolve authentication method". The buyer paid and got nothing.

    503, not 402: the buyer has done nothing wrong and paying would not help.
    """
    monkeypatch.setenv("SUPERVISOR_GENERATOR", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")  # whitespace is not a key
    monkeypatch.setenv("BUILD_WORKER_EMBEDDED", "0")
    broken = TestClient(create_app(verifier=DevPaymentVerifier(SECRET),
                                   store=InMemoryJobStore()))

    paid = broken.post("/builds", json=PRD_BODY, headers={PAYMENT_HEADER: SECRET})

    assert paid.status_code == 503
    assert broken.get("/healthz").json()["generator_ready"] is False


def test_a_usable_deployment_reports_itself_ready(monkeypatch):
    """The offline generators need no credentials, so they are always ready —
    otherwise this check would refuse the configuration used to sell the first
    builds."""
    monkeypatch.setenv("SUPERVISOR_GENERATOR", "template")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("BUILD_WORKER_EMBEDDED", "0")
    ok = TestClient(create_app(verifier=DevPaymentVerifier(SECRET),
                               store=InMemoryJobStore()))

    assert ok.get("/healthz").json()["generator_ready"] is True
    assert ok.post("/builds", json=PRD_BODY, headers={PAYMENT_HEADER: SECRET}).status_code == 202


def _wait(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/builds/{job_id}").json()
        if body["status"] in {"succeeded", "failed"}:
            return body
        time.sleep(0.05)
    raise AssertionError(f"build {job_id} did not finish within {timeout}s")


# --------------------------------------------------------------------------- #
# Payment
# --------------------------------------------------------------------------- #


def test_unpaid_request_is_402_with_a_challenge(client):
    response = client.post("/builds", json=PRD_BODY)

    assert response.status_code == 402
    body = response.json()
    assert body["x402Version"] == 1
    assert body["accepts"][0]["scheme"] == "exact"
    assert PAYMENT_HEADER in body["hint"]


def _rpc(client, method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        body["id"] = request_id
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body)


def test_an_agent_can_discover_and_call_the_tools_over_mcp(monkeypatch, tmp_path):
    """The whole point of the endpoint: a URL pasted into Claude or Cursor.

    Checked end to end rather than per-function, because what breaks a remote
    MCP server is never the tool body — it is the handshake around it.
    """
    client = _configured_client(monkeypatch, tmp_path)

    started = _rpc(client, "initialize", {"protocolVersion": "2025-06-18"}).json()
    assert started["result"]["protocolVersion"] == "2025-06-18"
    assert started["result"]["capabilities"]["tools"] is not None
    assert started["result"]["serverInfo"]["name"]

    listed = _rpc(client, "tools/list").json()["result"]["tools"]
    names = {tool["name"] for tool in listed}
    assert {"validate_prd", "prd_schema", "payment_terms", "build_status"} <= names
    for tool in listed:
        assert tool["description"] and tool["inputSchema"]["type"] == "object"

    called = _rpc(client, "tools/call", {
        "name": "validate_prd", "arguments": {"prd": PRD_BODY},
    }).json()["result"]
    assert called["isError"] is False
    assert json.loads(called["content"][0]["text"])["valid"] is True


def test_mcp_never_answers_a_notification(monkeypatch, tmp_path):
    """A message with no `id` is a notification and must get no response body.

    Clients send `notifications/initialized` immediately after the handshake.
    Replying to it is a protocol violation, and the strict ones treat that as a
    hard failure — which presents as a server that never finishes initialising
    rather than as a bad reply.
    """
    client = _configured_client(monkeypatch, tmp_path)

    response = _rpc(client, "notifications/initialized", request_id=None)

    assert response.status_code == 202
    assert not response.content


def test_mcp_reports_a_failing_tool_without_breaking_the_session(monkeypatch, tmp_path):
    """A tool that raises is a tool error, not a protocol error.

    An agent should see what went wrong and be able to try something else; a
    JSON-RPC error at the transport level makes the whole connection look
    broken instead.
    """
    client = _configured_client(monkeypatch, tmp_path)

    missing = _rpc(client, "tools/call", {
        "name": "build_status", "arguments": {"job_id": "does-not-exist"},
    }).json()
    assert "error" not in missing, "a bad job id must not fail the session"
    assert missing["result"]["isError"] is True

    unknown = _rpc(client, "tools/call", {"name": "no_such_tool", "arguments": {}}).json()
    assert unknown["error"]["code"] == -32602

    assert _rpc(client, "resources/list").json()["error"]["code"] == -32601, \
        "unimplemented methods must be refused, not silently accepted"


def test_a_document_that_validates_free_is_not_rejected_after_paying(monkeypatch, tmp_path):
    """The free preview has to agree with the paid path or it is a trap.

    Its whole purpose is letting a buyer check a document before spending $3.
    If /validate accepted something /builds then rejected with a 422, the free
    endpoint would be actively worse than nothing — so both run the same
    validator, and this pins that rather than trusting it.
    """
    client = _configured_client(monkeypatch, tmp_path)

    good = client.post("/validate", json=PRD_BODY).json()
    assert good["valid"] is True
    assert good["app_name"] == PRD_BODY["app_name"]
    assert good["would_build"]["screens"], "a buyer learns nothing without this"
    assert good["price"]["amount"] == "3.00"
    # The paid route accepts what the free one blessed: unpaid, so it reaches
    # the payment gate (402) rather than the schema rejection (422).
    assert client.post("/builds", json=PRD_BODY).status_code == 402

    bad = client.post("/validate", json={"app_name": "x"}).json()
    assert bad["valid"] is False and bad["errors"]
    assert client.post("/builds", json={"app_name": "x"}).status_code == 422


def test_llms_txt_tells_an_agent_the_price_and_the_free_routes(monkeypatch, tmp_path):
    """Indexed by directories and read by crawlers, so a stale price here is a
    lie told at scale. Generated from the live config for that reason."""
    body = _configured_client(monkeypatch, tmp_path).get("/llms.txt").text

    assert "3.00 USDC" in body
    assert "/validate" in body and "/schema/prd.json" in body
    assert "{price}" not in body and "{base}" not in body


def test_an_unpaid_get_on_the_paid_route_quotes_the_price(monkeypatch, tmp_path):
    """Crawlers, x402 clients and directory probers all GET first.

    Building needs a POST, so GET used to answer 405 — which reads as a broken
    or non-x402 endpoint rather than as a paid one. A discovery directory
    refused to verify the listing for precisely this, and an unverified listing
    ranks last among thousands. The terms are the same ones the POST quotes.
    """
    client = _configured_client(monkeypatch, tmp_path)

    unpaid = client.get("/builds")

    assert unpaid.status_code == 402, "a prober reads anything else as not-x402"
    assert unpaid.json()["accepts"][0]["maxAmountRequired"] == "3000000"


def test_the_front_door_answers_humans_and_machines(monkeypatch, tmp_path):
    """The root served `{"detail":"Not Found"}` to everyone who arrived.

    That is what a browser, a crawler and an agent all saw first, on a service
    whose entire purpose is being found and paid by strangers. Both answers are
    generated from the live config, so neither can advertise a stale price.
    """
    client = _configured_client(monkeypatch, tmp_path)

    page = client.get("/", headers={"accept": "text/html"})
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "3.00" in page.text, "a visitor cannot see what it costs"
    assert "{price}" not in page.text, "an unformatted placeholder reached the page"

    machine = client.get("/", headers={"accept": "application/json"}).json()
    assert machine["price_usdc"] == "3.00"
    assert machine["manifest"].endswith("/.well-known/x402")

    assert "Disallow: /builds/" in client.get("/robots.txt").text


def test_what_is_sent_at_settlement_is_a_usable_catalog_entry(monkeypatch):
    """The settlement body is this service's public listing.

    A facilitator catalogs an endpoint the first time it settles a payment for
    it, reading the entry out of `paymentRequirements` — there is no separate
    registration call, and no second chance that does not involve another
    payment. A `resource` of "/builds" would enter a 25,000-entry public index
    as an unresolvable path, and an entry without `outputSchema` gives an agent
    nothing to match against but prose.
    """
    from src.payments.eip3009 import Authorization, TokenConfig, VerifiedPayment
    from src.payments.facilitator import HttpFacilitator

    monkeypatch.setenv("PUBLIC_HOSTNAME", "example.test")
    token = TokenConfig(chain_id=8453, verifying_contract="0x" + "ab" * 20,
                        domain_name="USD Coin", network="base")
    facilitator = HttpFacilitator(
        "https://facilitator.invalid", token,
        pay_to="0x" + "cd" * 20,
        price_atomic=3_000_000,
        resource="https://example.test/builds",
        output_schema={"type": "object"},
    )
    payment = VerifiedPayment(
        authorization=Authorization(
            sender="0x" + "11" * 20, recipient="0x" + "cd" * 20,
            value=3_000_000, valid_after=0, valid_before=0, nonce=b"\x00" * 32,
        ),
        token=token, raw_header="", payload={},
    )

    listed = facilitator._body(payment)["paymentRequirements"]

    assert listed["resource"].startswith("https://"), \
        f"the catalog would list {listed['resource']!r}, which no agent can fetch"
    assert listed["outputSchema"], "an entry with no schema is one agents cannot use"
    assert listed["asset"].startswith("0x"), "the catalog needs the token contract"
    assert "$3.00" in listed["description"]


def test_an_agent_can_learn_the_terms_without_provoking_a_402(monkeypatch, tmp_path):
    """Discovery has to work before the first request, not as a side effect.

    An agent that must POST a real PRD to find out the price is reading terms
    off an error it caused. The manifest carries the same `accepts` entry the
    402 does — the same code path, so the two cannot describe different prices —
    plus the schema of what it would be buying.
    """
    client = _configured_client(monkeypatch, tmp_path)

    manifest = client.get("/.well-known/x402").json()
    quoted = client.post("/builds", json=PRD_BODY).json()

    assert manifest["accepts"] == quoted["accepts"], \
        "the advertised terms and the charged terms must be the same object"
    assert manifest["resource"].endswith("/builds"), \
        f"the manifest must point at the payable endpoint, not itself"
    assert manifest["accepts"][0]["asset"].startswith("0x")

    schema = client.get("/schema/prd.json").json()
    for required in ("app_name", "package_name", "screens"):
        assert required in schema["properties"], \
            f"{required} is rejected by the API but absent from the published schema"


def _configured_client(monkeypatch, tmp_path, atomic=3_000_000):
    """A service with a real token configured, which is what a buyer meets.

    The plain `client` fixture runs the dev verifier with no token at all, so its
    challenge legitimately omits every chain field — a useful default for the
    rest of the suite, and useless for checking what the challenge must contain.
    """
    monkeypatch.setenv("SUPERVISOR_GENERATOR", "template")
    monkeypatch.setenv("X402_TOKEN_CONTRACT", "0x036CbD53842c5426634e7929541eC2318f3dCF7e")
    monkeypatch.setenv("X402_CHAIN_ID", "84532")
    monkeypatch.setenv("X402_NETWORK", "base-sepolia")
    monkeypatch.setenv("X402_PAY_TO", "0x000000000000000000000000000000000000dEaD")
    monkeypatch.setenv("X402_PRICE_ATOMIC", str(atomic))
    monkeypatch.setattr("src.service.app.BUILD_ROOT", tmp_path)
    return TestClient(create_app())


def test_the_challenge_carries_every_field_the_x402_spec_requires(monkeypatch, tmp_path):
    """Written from the v1 spec's field table, not from what we happen to emit.

    The first version of this challenge was validated only against our own
    client, and the two agreed with each other rather than with the standard:
    `asset` held the symbol "USDC" where the spec requires the token contract
    address, required `maxTimeoutSeconds` was absent, and `resource` was a path
    where a URL is specified. Nothing failed, because the only client that ever
    read it was the one written alongside it — so the service was unpayable by
    every third-party agent while all 369 tests passed.
    """
    body = _configured_client(monkeypatch, tmp_path).post("/builds", json=PRD_BODY).json()
    assert body["x402Version"] == 1, "the facilitator's /supported lists v1 only"

    terms = body["accepts"][0]
    for field in ("scheme", "network", "maxAmountRequired", "asset", "payTo",
                  "resource", "description", "maxTimeoutSeconds"):
        assert field in terms, f"the spec requires {field}"

    assert terms["asset"].startswith("0x") and len(terms["asset"]) == 42, \
        f"asset must be the token contract address, got {terms['asset']!r}"
    assert terms["maxAmountRequired"].isdigit(), "atomic units, as a string"
    assert terms["resource"].startswith("http"), \
        f"resource must be a URL a buyer can fetch, got {terms['resource']!r}"
    assert isinstance(terms["maxTimeoutSeconds"], int)
    # v2 defines `amount` as atomic units. Emitting a decimal price under that
    # name would read to a spec-following client as three millionths of a dollar.
    assert "amount" not in terms


@pytest.mark.parametrize("atomic", [3_000_000, 1_250_000, 10_000_000])
def test_the_advertised_price_is_the_price_that_is_enforced(monkeypatch, tmp_path, atomic):
    """The quote states the price twice — once for a human, once for the
    signature — and nothing made them agree.

    `amount` was its own parameter with a "0.50" default that no caller ever
    passed, so when the enforced price moved to $3.00 the 402 went on
    advertising fifty cents. A buyer agent that shows `amount` and signs
    `maxAmountRequired` displays one price and pays six times it.

    Every price here is deliberately *not* 500000. At the default price the
    stale constant and the real one agree by coincidence, and this test passes
    with the bug fully present — which is how it first read as green.
    """
    client = _configured_client(monkeypatch, tmp_path, atomic)

    accepts = client.post("/builds", json=PRD_BODY).json()["accepts"][0]

    assert int(accepts["maxAmountRequired"]) == atomic
    # The price in dollars is prose now — there is no spec field for it that does
    # not already mean something else — but it is still derived from the enforced
    # figure, so the two cannot drift apart.
    stated = re.search(r"\$([\d.]+) USDC", accepts["description"])
    assert stated, f"no price stated in {accepts['description']!r}"
    assert Decimal(stated.group(1)) * (10 ** 6) == atomic, (
        f"advertises {stated.group(1)} USDC but enforces {atomic} atomic units"
    )


def test_wrong_payment_is_rejected(client):
    response = client.post("/builds", json=PRD_BODY, headers={PAYMENT_HEADER: "nope"})
    assert response.status_code == 402


def test_buyer_cannot_self_certify_payment(client):
    """The critical one.

    `x402_payment_verified` is a PRD field and the PRD is buyer-supplied. If the
    submitted value were trusted, anyone could set it to true and receive a paid
    build for nothing. It must be discarded server-side.
    """
    forged = {**PRD_BODY, "x402_payment_verified": True}
    response = client.post("/builds", json=forged)

    assert response.status_code == 402, "a forged flag must not buy a build"


def test_server_sets_the_flag_once_payment_settles():
    """And having settled, the pipeline must see it as verified."""
    prd = load_prd("examples/todo_app.prd.json")
    assert prd.x402_payment_verified is False

    assert _verified_prd(prd)["x402_payment_verified"] is True


def test_verifier_fails_closed_with_no_secret_configured():
    """A misconfigured deployment must refuse payment, not grant it."""
    assert DevPaymentVerifier(None).settle("anything") is False
    assert DevPaymentVerifier("").settle("") is False


# --------------------------------------------------------------------------- #
# Build lifecycle
# --------------------------------------------------------------------------- #


def test_malformed_prd_is_422_and_is_not_charged(client):
    """Validate before charging: taking money for work that cannot start is theft."""
    response = client.post(
        "/builds",
        json={"app_name": "X", "package_name": "not-reverse-dns", "screens": []},
        headers={PAYMENT_HEADER: SECRET},
    )
    assert response.status_code == 422


def test_paid_build_is_accepted_and_runs_to_completion(client):
    response = client.post("/builds", json=PRD_BODY, headers={PAYMENT_HEADER: SECRET})

    assert response.status_code == 202
    assert response.headers["Location"].endswith(response.json()["id"])

    body = _wait(client, response.json()["id"])
    assert body["status"] == "succeeded", body.get("failure") or body["diagnostics"]
    assert body["log"], "the buyer should get the build log"
    assert body["finished_at"]


def test_unknown_build_is_404(client):
    assert client.get("/builds/does-not-exist").status_code == 404
    assert client.get("/builds/does-not-exist/apk").status_code == 404


def test_apk_is_409_while_the_build_is_unfinished(client):
    """The build exists; it simply has no artifact yet. That is not a 404."""
    store = InMemoryJobStore()
    job = store.create("Pending")
    app = create_app(verifier=DevPaymentVerifier(SECRET), store=store)

    with TestClient(app) as pending:
        response = pending.get(f"/builds/{job.id}/apk")

    assert response.status_code == 409
    assert job.status is BuildStatus.QUEUED


def test_succeeded_build_without_an_apk_is_404_not_a_broken_download(client):
    response = client.post("/builds", json=PRD_BODY, headers={PAYMENT_HEADER: SECRET})
    job_id = response.json()["id"]
    _wait(client, job_id)

    # The stub analyzer path never packages, so there is no artifact to serve.
    apk = client.get(f"/builds/{job_id}/apk")
    assert apk.status_code == 404
    assert "packaging" in apk.json()["detail"]


def test_unconfigured_deployment_refuses_every_payment(monkeypatch):
    """Fail closed. A service that cannot verify payment must not accept it —
    defaulting to the dev shared secret would sell builds to anyone who guesses
    a string."""
    for var in ("X402_SHARED_SECRET", "X402_TOKEN_CONTRACT",
                "X402_CHAIN_ID", "X402_PAY_TO"):
        monkeypatch.delenv(var, raising=False)
    # This test is about payment, not generation. Without it the default
    # generator is `claude`, which needs a key, and the request is refused as
    # unbuildable before the payment gate is ever consulted — passing for a
    # reason that has nothing to do with what is being asserted.
    monkeypatch.setenv("SUPERVISOR_GENERATOR", "template")

    with TestClient(create_app()) as bare:
        health = bare.get("/healthz").json()
        assert health["payment_configured"] is False
        assert health["payment_mode"] == "none"

        response = bare.post("/builds", json=PRD_BODY, headers={PAYMENT_HEADER: "anything"})
        assert response.status_code == 402


def test_healthz_distinguishes_verification_from_settlement(monkeypatch):
    """Accepting signed-but-unsettled promises is a deployment choice, and an
    operator should be able to see it without reading the source."""
    monkeypatch.setenv("X402_SHARED_SECRET", "s")
    monkeypatch.delenv("X402_FACILITATOR_URL", raising=False)

    with TestClient(create_app()) as dev:
        body = dev.get("/healthz").json()
        assert body["payment_mode"] == "dev-shared-secret"
        assert body["settlement"] == "verification-only"


def test_challenge_tells_the_buyer_how_to_pay(monkeypatch):
    """A 402 without the recipient, chain and token is unactionable — the client
    would have to guess exactly the fields where a wrong guess still produces a
    valid signature, for the wrong thing."""
    monkeypatch.setenv("X402_TOKEN_CONTRACT", "0x036CbD53842c5426634e7929541eC2318f3dCF7e")
    monkeypatch.setenv("X402_CHAIN_ID", "84532")
    monkeypatch.setenv("X402_PAY_TO", "0x000000000000000000000000000000000000dEaD")
    monkeypatch.setenv("X402_NETWORK", "base-sepolia")
    monkeypatch.setenv("SUPERVISOR_GENERATOR", "template")  # see the test above

    with TestClient(create_app()) as paid:
        body = paid.post("/builds", json=PRD_BODY).json()

    accepts = body["accepts"][0]
    assert accepts["network"] == "base-sepolia"
    assert accepts["payTo"] == "0x000000000000000000000000000000000000dEaD"
    # The token contract lives in `asset`, which is the field the spec tells a
    # buyer to read. It used to be in `verifyingContract`, a name this service
    # invented, with the symbol "USDC" sitting in `asset` instead.
    assert accepts["asset"].lower().startswith("0x036cbd")
    assert accepts["extra"]["version"] == "2"
    assert body["error"], "the buyer should be told why this attempt failed"


# --------------------------------------------------------------------------- #
# Job store
# --------------------------------------------------------------------------- #


def _worker(store, queue, work):
    return BuildWorker(store, queue, lambda _job: work)


def test_a_crashing_build_never_stays_running():
    store, queue = InMemoryJobStore(), InMemoryBuildQueue()
    job = store.create("Boom")
    queue.push(job.id)

    def explode(_job):
        raise RuntimeError("gradle fell over")

    _worker(store, queue, explode).run_once()

    assert job.status is BuildStatus.FAILED
    assert "gradle fell over" in job.failure


def test_work_that_reports_no_outcome_is_treated_as_failure():
    store, queue = InMemoryJobStore(), InMemoryBuildQueue()
    job = store.create("Silent")
    queue.push(job.id)

    _worker(store, queue, lambda _job: None).run_once()

    assert job.status is BuildStatus.FAILED
    assert "without reporting an outcome" in job.failure


def test_a_terminal_status_never_becomes_visible_without_finished_at(
    monkeypatch, tmp_path
):
    """`finished_at` must be set in the same breath as the terminal status.

    `InMemoryJobStore` hands out the live `BuildJob`, so the moment the build
    closure sets `status` to a terminal value a concurrent `GET /builds/{id}`
    can see it — and artifact pruning runs after that, doing real filesystem
    work. `_execute` filling in `finished_at` only once the closure returns
    leaves a window where the API reports a succeeded build with a null
    `finished_at`, which CI's Linux runner hit on the first push and this
    machine reproduced 40 times out of 40.

    Calling the closure directly tests the invariant without racing anything:
    when it returns, both fields are already set.
    """
    monkeypatch.setenv("SUPERVISOR_GENERATOR", "template")
    monkeypatch.setenv("SUPERVISOR_ANALYZER", "stub")
    monkeypatch.setenv("SUPERVISOR_RUN_TESTS", "0")

    store = InMemoryJobStore()
    job = store.create("Timestamps", prd=PRD_BODY)
    job.paid = True
    job.build_dir = str(tmp_path / job.id)

    build_work(job)(job)

    assert job.status in {BuildStatus.SUCCEEDED, BuildStatus.FAILED}
    assert job.finished_at is not None, (
        "the build closure must stamp finished_at itself; leaving it to the "
        "worker exposes a terminal status with a null timestamp"
    )
