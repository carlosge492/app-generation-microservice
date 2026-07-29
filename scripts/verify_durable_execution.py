"""Prove that a paid build survives the worker that accepted it.

    poetry run python scripts/verify_durable_execution.py

The unit tests prove the queue's semantics in one interpreter. This proves the
claim the buyer actually cares about, across real operating-system processes:

  1. an API process accepts and pays for a build, and does not run it
  2. a worker process reserves it and starts building
  3. that worker is killed outright, mid-build, with no chance to clean up
  4. a second worker notices the lapsed lease and finishes the build
  5. the buyer polls one job id throughout and gets an APK-shaped success

Step 3 is `TerminateProcess`/`SIGKILL`, not a polite shutdown, because a polite
shutdown is the case that was never in doubt. Nothing in the dying process gets
to hand anything over: recovery has to come from state that outlived it, which
is the whole point.

**On the Redis.** There is no Redis daemon on this machine, so this uses
`fakeredis`'s `TcpFakeServer` — a real TCP socket speaking real RESP, which the
child processes reach through the ordinary `redis` client over the loopback
interface. The process boundaries, the sockets, the serialisation and the kill
are all genuine; the server implementing `LMOVE` and key expiry is not the
real one. That is a weaker claim than "verified against Redis 8" and is recorded
as such in the README rather than rounded up.

The build uses the template generator and the real Dart analyzer, which takes
about a minute and a half — long enough that killing a worker mid-build is
reliable rather than a race the script has to win.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
REDIS_PORT = 6399
API_PORT = 8099
API = f"http://127.0.0.1:{API_PORT}"
SECRET = "durability-check"
PRD = ROOT / "examples" / "todo_app.prd.json"

# Short enough that the demonstration does not take all afternoon; a real
# deployment wants the defaults, which tolerate a slow Gradle step.
LEASE_SECONDS = 10
HEARTBEAT_SECONDS = 3


def log(message: str) -> None:
    print(f"  {message}", flush=True)


def start_redis() -> object:
    from fakeredis import TcpFakeServer

    class QuietTcpFakeServer(TcpFakeServer):
        """Killing a worker resets its Redis socket, which is the whole point.

        The stock handler prints a traceback per dropped connection, so the one
        line of evidence this script exists to produce arrives buried under
        several screens of `ConnectionResetError`.
        """

        def handle_error(self, request, client_address) -> None:
            if isinstance(sys.exc_info()[1], (ConnectionResetError, ConnectionAbortedError)):
                return
            super().handle_error(request, client_address)

    server = QuietTcpFakeServer(("127.0.0.1", REDIS_PORT), server_type="redis")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"redis (fakeredis TCP) listening on 127.0.0.1:{REDIS_PORT}")
    return server


def child_env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}",
            "X402_SHARED_SECRET": SECRET,
            "SUPERVISOR_GENERATOR": "template",
            "SUPERVISOR_ANALYZER": "dart",
            "SUPERVISOR_RUN_TESTS": "0",
            "FLUTTER_ROOT": os.getenv("FLUTTER_ROOT", r"C:\flutter"),
            "BUILD_ROOT": str(ROOT / "generated_apps" / "durability"),
            "BUILD_LEASE_SECONDS": str(LEASE_SECONDS),
            "BUILD_HEARTBEAT_SECONDS": str(HEARTBEAT_SECONDS),
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.update(overrides)
    return env


def spawn(args: list[str], env: dict[str, str], name: str) -> subprocess.Popen:
    log(f"starting {name}: {' '.join(args)}")
    return subprocess.Popen(
        [sys.executable, *args],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def kill(process: subprocess.Popen) -> None:
    """No SIGTERM, no grace period. The worker gets no chance to hand over."""
    process.kill()
    process.wait(timeout=30)


def wait_for(predicate, timeout: float, description: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.25)
    raise SystemExit(f"FAILED: timed out after {timeout}s waiting for {description}")


def status(job_id: str) -> dict:
    return httpx.get(f"{API}/builds/{job_id}", timeout=10).json()


def health() -> dict | None:
    try:
        response = httpx.get(f"{API}/healthz", timeout=2)
    except httpx.HTTPError:
        return None
    return response.json() if response.status_code == 200 else None


def finished(job_id: str) -> dict | None:
    body = status(job_id)
    return body if body["status"] in {"succeeded", "failed"} else None


def main() -> int:
    print("\nDurable build execution — across processes\n" + "=" * 42)
    start_redis()

    api = spawn(
        ["-m", "uvicorn", "src.service.app:app", "--port", str(API_PORT)],
        # The API accepts and queues; it must not be the thing that builds, or
        # killing a worker would prove nothing.
        child_env(BUILD_WORKER_EMBEDDED="0"),
        "API process",
    )
    worker_a = None
    worker_b = None

    try:
        ready = wait_for(health, 60, "the API to come up")
        if not ready["durable_execution"]:
            raise SystemExit(f"FAILED: durable_execution is false: {ready}")
        log(f"healthz: durable_execution={ready['durable_execution']}, "
            f"job_store={ready['job_store']}, embedded_worker={ready['embedded_worker']}")

        # -- the buyer pays ------------------------------------------------- #
        accepted = httpx.post(
            f"{API}/builds",
            json=json.loads(PRD.read_text(encoding="utf-8")),
            headers={"X-PAYMENT": SECRET},
            timeout=30,
        )
        if accepted.status_code != 202:
            raise SystemExit(f"FAILED: expected 202, got {accepted.status_code}")
        job_id = accepted.json()["id"]
        log(f"paid build accepted as {job_id}; queued, not running")

        # -- worker A takes it, and dies ------------------------------------ #
        worker_a = spawn(["-m", "src.service.worker"], child_env(), "worker A")
        wait_for(
            lambda: status(job_id)["status"] == "running", 120, "worker A to start building"
        )
        log("worker A is building; killing it mid-build")
        time.sleep(5)  # let it get properly underway
        kill(worker_a)
        worker_a = None

        after_death = status(job_id)
        log(f"worker A is gone; build reports {after_death['status']} "
            f"(attempt {after_death['attempts']})")

        # -- worker B recovers it ------------------------------------------- #
        worker_b = spawn(["-m", "src.service.worker"], child_env(), "worker B")
        wait_for(
            lambda: status(job_id)["attempts"] >= 2,
            LEASE_SECONDS + 60,
            "worker B to requeue and reserve the abandoned build",
        )
        log("worker B picked up the abandoned build")

        final = wait_for(
            lambda: finished(job_id), 600, "worker B to finish the build"
        )

        # -- what has to be true -------------------------------------------- #
        if final["status"] != "succeeded":
            raise SystemExit(
                f"FAILED: build ended {final['status']}: {final.get('failure')}"
            )
        if final["attempts"] < 2:
            raise SystemExit("FAILED: the retry was never recorded on the job")
        if not any("abandoned" in line for line in final["log"]):
            raise SystemExit("FAILED: the log does not mention the abandoned attempt")

        print("\n" + "=" * 42)
        print("PASSED: a paid build survived the death of the worker running it.")
        print(f"  job          {job_id}")
        print(f"  attempts     {final['attempts']} (the first was killed mid-build)")
        print(f"  status       {final['status']}")
        print(f"  first log    {final['log'][0]}")
        return 0

    finally:
        for process, name in ((worker_a, "worker A"), (worker_b, "worker B"), (api, "API")):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=30)
                log(f"stopped {name}")


if __name__ == "__main__":
    sys.exit(main())
