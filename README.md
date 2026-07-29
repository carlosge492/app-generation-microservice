# App-Generation Microservice

Takes a JSON Product Requirements Document and emits a compiled Flutter
application. Built as a LangGraph loop with four subagents, a QA gate that runs
the real Flutter toolchain, and an x402 payment gate in front of packaging.

The design goal is not "generates plausible code" but **zero-error compilation**:
if the output fails CI, the loop has failed.

## Quick start

No credentials and no Flutter SDK needed — the defaults are fully offline:

```bash
poetry install
poetry run python src/supervisor.py examples/todo_app.prd.json --clean
```

That validates the PRD, runs the loop, and writes a Flutter project to
`generated_apps/current_build/`.

To grade the output with the real toolchain instead of the offline stub:

```bash
poetry run python src/supervisor.py --clean --analyzer dart --flutter-root "C:\flutter" --run-tests
```

## As a service

CLAUDE.md calls this an M2M microservice; this is the part a machine buyer calls.

```bash
X402_SHARED_SECRET=... FLUTTER_ROOT="C:\flutter"   poetry run uvicorn src.service.app:app --port 8000
```

| Endpoint | |
| --- | --- |
| `POST /builds` | PRD in; `202` + job id, or `402` with an x402 challenge |
| `GET /builds/{id}` | status, log, diagnostics |
| `GET /builds/{id}/apk` | the artifact |
| `GET /healthz` | includes whether payment is configured |

Builds take minutes, so the API is asynchronous. **`x402_payment_verified` in a
submitted PRD is discarded**: the PRD is buyer-supplied, so trusting that field
would let anyone assert their own payment. Only the server's verifier sets it.

Payment is real x402: the buyer signs an EIP-3009 `TransferWithAuthorization`
(EIP-712), and the service recovers the signer, checks recipient, amount, chain
and validity window, and claims the nonce atomically so one signature buys
exactly one build.

```bash
X402_TOKEN_CONTRACT=0x036CbD...  X402_CHAIN_ID=84532 X402_PAY_TO=0xYourAddress        X402_PRICE_ATOMIC=500000   poetry run uvicorn src.service.app:app --port 8000
```

Configure nothing and the service refuses every payment — it fails closed rather
than falling back to the development shared secret. `/healthz` reports which
mode is active.

**Verification is not settlement.** A valid signature proves the payer
*authorised* a transfer; it does not prove they hold the balance or that the
transfer landed on-chain. Set `X402_FACILITATOR_URL` and the service submits the
authorization for on-chain execution and blocks the `202` until it confirms —
so a build only starts once the money has actually moved. Without it,
`/healthz` reports `settlement: verification-only` and the service is accepting
signed promises.

Before the nonce is claimed the service asks the facilitator's `/verify`
endpoint whether the payment would settle. Insufficient funds is recoverable —
the payer tops up and re-presents the same signature — so burning their
authorization for it would be needlessly destructive.

Settlement distinguishes three outcomes, not two. A refusal ("insufficient
funds") is definite and is not retried. A timeout is *unknown*: the facilitator
may have broadcast and failed to answer, so the build is refused while
recording that the buyer may have been charged. Retrying a transport failure is
safe — an EIP-3009 nonce is single-use on-chain, so a duplicate submission
reverts rather than charging twice.

### A working testnet configuration

Verified end to end on Base Sepolia — a real transaction, a real APK:

```bash
export X402_TOKEN_CONTRACT=0x036CbD53842c5426634e7929541eC2318f3dCF7e  # USDC
export X402_CHAIN_ID=84532
export X402_NETWORK=base-sepolia
export X402_PRICE_ATOMIC=500000                                       # 0.50 USDC
export X402_PAY_TO=0xYourReceivingAddress
export X402_FACILITATOR_URL=https://facilitator.payai.network         # public, no auth
export FLUTTER_ROOT="C:\flutter"
poetry run uvicorn src.service.app:app --port 8000
```

The payer needs USDC only — **no ETH**. The facilitator submits the transaction
and pays gas, which is the point of EIP-3009; if a wallet is being asked for
testnet ETH, something is wired wrong. Faucet: <https://faucet.circle.com>.

`scripts/check_x402_funding.py` reports whether a payer is funded by asking the
facilitator's `/verify`, rather than introducing a second source of truth that
could disagree with the one the gate actually uses.

### Running more than one worker

Set `REDIS_URL` and both the nonce store and the job store move to Redis;
`/healthz` reports `multi_process_safe`. Nonce consumption uses `SET NX EX`,
one atomic round trip, so the time-of-check/time-of-use protection holds across
workers rather than only within one interpreter. A Redis outage refuses payment
rather than allowing replays.

### Surviving a restart

The money moves on-chain before the build starts, so losing a build to a deploy
means having taken payment for nothing. Execution is therefore durable, not just
job *state*: `POST /builds` settles the payment, writes the PRD onto the job
record, and pushes the id onto a queue. Workers pull from it.

```bash
BUILD_WORKER_EMBEDDED=0 poetry run uvicorn src.service.app:app --port 8000  # accepts
poetry run python -m src.service.worker                                     # builds
```

An API process builds as well as accepts by default, so a single-container
deployment needs neither of those flags; set `BUILD_WORKER_EMBEDDED=0` once
requests and builds want scaling apart.

A worker holds a lease on the job it is building and renews it while it works.
Kill the worker and the lease lapses, another worker returns the job to the
queue, and it is rebuilt from the stored PRD — which is why the PRD is stored
rather than captured in a closure, as it was when execution lived inside the
accepting process. `scripts/verify_durable_execution.py` demonstrates exactly
that, across real processes and a real `SIGKILL`.

Two things this deliberately does not promise. A lease **bounds** duplicate work
rather than preventing it: a worker partitioned from Redis for longer than its
lease is indistinguishable from a dead one, so its build may be handed on while
it is still running. `renew` returning False is how the stalled worker learns to
discard its result, but the overlap is real, and it costs compute rather than
money — the payment settled once, before the job was queued. And retries are
**bounded** (`BUILD_MAX_ATTEMPTS`, default 3): without that, one build that
reliably kills its worker would be requeued into the next one and take the fleet
down a process at a time.

| Variable | Default | What it is for |
| --- | --- | --- |
| `BUILD_WORKER_EMBEDDED` | `1` | whether the API process also builds |
| `BUILD_LEASE_SECONDS` | `300` | how long a silent worker keeps its job |
| `BUILD_HEARTBEAT_SECONDS` | `30` | how often a working worker says so |
| `BUILD_MAX_ATTEMPTS` | `3` | before a build is given up on for good |

`/healthz` reports `durable_execution`, which is true only when the queue *and*
the job store are both shared — a Redis queue over an in-memory job store loses
the record the build would be resumed from.

## Commands

| What | Command |
| --- | --- |
| Build one app | `poetry run python src/supervisor.py <prd.json>` |
| Eval sweep (offline, ~1s) | `poetry run python evals/run.py` |
| Eval sweep (real analysis) | `poetry run python evals/run.py --analyzer dart --flutter-root "C:\flutter"` |
| Eval sweep (+ widget tests) | add `--run-tests` |
| Unit tests | `poetry run pytest` |
| Build worker (pulls from the queue) | `poetry run python -m src.service.worker` |
| Prove a build survives a killed worker | `poetry run python scripts/verify_durable_execution.py` |
| Check the x402 payer is funded | `poetry run python scripts/check_x402_funding.py` |

Useful flags: `--generator {template,claude}`, `--analyzer {stub,dart}`,
`--max-repairs N`, `--build-dir PATH`, `--clean`, `--execute`.

Poetry may not be on `PATH`; `python -m poetry ...` works either way. The Flutter
SDK is deliberately not on `PATH` — pass `--flutter-root` or set `$FLUTTER_ROOT`.

## How it works

```
plan ──> genui ──> logic ──> qa ──┬──> package ──> END
          ^          ^            │
          └──────────┴── repair ──┘        (bounded by --max-repairs)
```

Each subagent owns a strict slice of the output and may not write outside it —
enforced in code, not just documented:

| Path | Owner |
| --- | --- |
| `DESIGN.md`, `pubspec.yaml`, `analysis_options.yaml` | Planning |
| `lib/ui/**` | GenUI |
| `lib/providers/**`, `lib/main.dart` | Logic |
| `test/**` | QA (derived from the generated sources) |

QA diagnostics are routed to whichever agent can actually fix them. If a round
changes nothing, the router escalates to the other agent rather than spending
the whole repair budget re-running the one that already failed.

## The three generators

Both implement the same `CodeGenerator` port, so the graph does not know which
it has.

- **`template`** (default) — deterministic Python templates. No network, no
  credentials. This is the validated path.
- **`fixture`** — a test double emitting correct Flutter that shares none of
  `TemplateGenerator`'s naming conventions. Proves the grading harness is not
  merely grading its own output. See `evals/fixture_generator.py`.
- **`claude`** — real Claude calls with structured outputs. Validated on
  Opus 5 at 11/11 across the eval sweep.

## What is actually verified

Honest status, because "it compiles" and "it works" are different claims:

| Property | How | Status |
| --- | --- | --- |
| PRD contract | Pydantic, referential integrity | ✅ |
| Compiles / type-checks | real `flutter analyze` | ✅ 11/11 PRDs |
| Matches the PRD | `src/ports/conformance.py` | ✅ |
| No unwired/hallucinated code | analyzer + conformance | ✅ |
| Screens actually build at runtime | generated widget smoke tests | ✅ |
| Repair loop recovers honestly | fault injection | ✅ |
| x402 gate | unit tests | ✅ |
| EIP-712/EIP-3009 signature verification | real keys, 28 tests | ✅ |
| Replay protection | atomic claim, concurrent race test | ✅ |
| Facilitator wire format | live `facilitator.payai.network` | ✅ payload accepted, payer recovered |
| On-chain settlement | live, Base Sepolia | ✅ real tx, ~1.2s |
| Replay refused on-chain | store bypassed | ✅ `duplicate_settlement` |
| Multi-process replay safety | 2 uvicorn workers, shared Redis | ✅ replay refused across processes |
| Durable build execution | worker killed mid-build, across processes | ✅ rebuilt by another worker |
| Duplicate work bounded, not excluded | lease lapses under a stall | ⚠️ by design — see above |
| HTTP service, end to end | live server, PRD → APK download | ✅ |
| Buyer cannot self-certify payment | forged flag → 402 | ✅ |
| Claude generator — request shape | fake transport, 18 tests | ✅ |
| Harness works on non-template code | `--generator fixture` | ✅ full pipeline + APK path |
| Claude generator (Opus 5) | full eval sweep | ✅ 11/11 |
| APK packaging (template) | `flutter build apk --debug` | ✅ correct applicationId |
| APK packaging (Opus output) | `flutter build apk --debug` | ✅ 145 MB, x402-gated |
| Navigation / real Firestore I/O | — | ❌ needs an emulator |
| Release signing / Play upload | — | ❌ debug keystore only |

One caveat on the two Redis rows, since "✅" is doing real work there. There is
no Redis daemon on the development machine, so both were verified against
`fakeredis` — for the unit tests in-process, and for the cross-process durability
run over `TcpFakeServer`, a real TCP socket speaking real RESP. The process
boundaries, the sockets and the `SIGKILL` are genuine; the server implementing
`LMOVE` and key expiry is not. Running `scripts/verify_durable_execution.py`
against a real Redis is a `REDIS_URL` away and has not been done.

211 unit tests; eval sweep 11/11 against real analysis and widget tests;
a debug APK built end to end from a payment-verified PRD.

## Layout

```
src/
  supervisor.py        CLI entry point
  prd/schema.py        the PRD contract — strict, validates referential integrity
  graph/               LangGraph wiring, shared state, the five nodes
  ports/
    generator.py       CodeGenerator protocol
    templates.py       deterministic generator (validated)
    llm.py             Claude generator (Opus 5, validated)
    analyzer.py        StubAnalyzer (offline) + FlutterAnalyzer (real)
    conformance.py     does the app match the PRD?
    smoke.py           generates widget tests from the output
    runtime.py         runs them
    ownership.py       which agent can fix which diagnostic
  payments/x402.py     the payment gate + x402 verifier selection
  payments/eip3009.py  EIP-712 signature recovery and field checks
  payments/replay.py   single-use nonces, atomic claim
  service/
    app.py             FastAPI app; accepts and pays, then queues
    jobs.py            the job record (including the PRD) and its store
    queue.py           reliable queue: atomic reserve, leases, requeue
    worker.py          pull-based workers; heartbeats and bounded retries
  build/pipeline.py    APK packaging (x402-gated)
  build/scaffold.py    generates android/ via `flutter create`
evals/
  prds/                10 PRDs chosen to break things
  run.py               pass-rate harness
```

## Notes and limitations

- **Escaping is load-bearing.** PRD text crosses into two generated languages,
  and each boundary needs its own escaping: `yaml_scalar` for `pubspec.yaml`,
  `dart_string` for Dart literals. Both exist because both broke in testing.
- **The stub analyzer cannot parse Dart.** It enforces conventions and catches
  provider desync, but it passed a project with 59 syntax errors. Treat a green
  offline run as a smoke signal, not proof; use `--analyzer dart` for that.
- **Cupertino is a full template family**, not a swapped root widget — a
  `CupertinoApp` provides no `MaterialLocalizations`, so Material widgets throw
  at runtime inside one.
- **Smoke tests use stubbed providers.** They prove a screen builds and its data
  layer resolves; they do not prove navigation works or that writes reach
  Firestore.
