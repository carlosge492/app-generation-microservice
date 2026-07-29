"""HTTP surface tests.

No model calls and no toolchain: the generator is the offline template one and
the payment verifier is injected, so these run anywhere.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from src.payments.x402 import PAYMENT_HEADER, DevPaymentVerifier
from src.service.app import _verified_prd, create_app
from src.service.jobs import BuildStatus, JobStore
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
    return TestClient(create_app(verifier=DevPaymentVerifier(SECRET), store=JobStore()))


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
    store = JobStore()
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

    with TestClient(create_app()) as paid:
        body = paid.post("/builds", json=PRD_BODY).json()

    accepts = body["accepts"][0]
    assert accepts["network"] == "base-sepolia"
    assert accepts["chainId"] == 84532
    assert accepts["payTo"] == "0x000000000000000000000000000000000000dEaD"
    assert accepts["verifyingContract"].lower().startswith("0x036cbd")
    assert accepts["extra"]["version"] == "2"
    assert body["error"], "the buyer should be told why this attempt failed"


# --------------------------------------------------------------------------- #
# Job store
# --------------------------------------------------------------------------- #


def test_a_crashing_build_never_stays_running():
    store = JobStore()
    job = store.create("Boom")

    def explode(_job):
        raise RuntimeError("gradle fell over")

    store.submit(job, explode)
    deadline = time.time() + 5
    while job.status is not BuildStatus.FAILED and time.time() < deadline:
        time.sleep(0.02)

    assert job.status is BuildStatus.FAILED
    assert "gradle fell over" in job.failure


def test_work_that_reports_no_outcome_is_treated_as_failure():
    store = JobStore()
    job = store.create("Silent")
    store.submit(job, lambda _job: None)

    deadline = time.time() + 5
    while job.status is BuildStatus.QUEUED or job.status is BuildStatus.RUNNING:
        if time.time() > deadline:
            break
        time.sleep(0.02)

    assert job.status is BuildStatus.FAILED
    assert "without reporting an outcome" in job.failure
