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

### Release builds, and why the service will not sign them

`flutter create` scaffolds a release build type that signs with the **debug**
key, under a `// TODO: Add your own signing config` comment. Left alone,
`flutter build apk --release` succeeds, produces a plausible `app-release.apk`,
installs on a device, and cannot be published — Play rejects the debug key. A
buyer would find that out at upload, having already paid.

So `--build-mode release` strips that config and emits an **unsigned** APK.

```bash
poetry run python src/supervisor.py <prd.json> --execute \
  --build-mode release --flutter-root "C:\flutter" --sdk-root "C:\Android"
```

The service does not sign, and holds no keys. A release key decides who can ship
updates to an app's installed base; holding buyers' keys would mean holding that
power over every app ever generated here, and one breach would compromise all of
them together. The buyer signs with a key this service never sees:

```bash
zipalign -p -f 4 app-release-unsigned.apk app-release.apk
apksigner sign --ks my-release-key.jks --ks-key-alias mykey app-release.apk
apksigner verify --print-certs app-release.apk
```

Two details worth knowing. The Gradle edit is **best-effort and not the
guarantee** — pattern-matching a template that changes between Flutter versions
is not something to stake a security property on — so the pipeline then inspects
the APK that actually came out and *fails* rather than hand over a release build
it cannot show to be unsigned. That check needs `apksigner`, so release builds
require `--sdk-root` or `ANDROID_SDK_ROOT`; without it the state is `unknown`,
and unknown is refused, because guessing in the reassuring direction is how a
debug-signed APK reaches a buyer labelled as a release build.

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
| Prove a release APK is unsigned and signable | `poetry run python scripts/verify_release_signing.py` |
| Prove a generated app really talks to Firestore | `poetry run python scripts/verify_firestore_roundtrip.py` |
| Check the x402 payer is funded | `poetry run python scripts/check_x402_funding.py` |

Useful flags: `--generator {template,claude}`, `--analyzer {stub,dart}`,
`--max-repairs N`, `--build-dir PATH`, `--clean`, `--execute`,
`--build-mode {debug,release}`, `--sdk-root PATH`.

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
| The generated app starts at all | `main()` executed in Chrome | ✅ was broken in every app |
| Real Firestore read/write | emulator + Chrome, generated app | ✅ template generator, web only |
| Generated date fields survive a round trip | same | ✅ was broken; see below |
| Every generated app ships a round-trip test | generated from the PRD in the QA phase | ✅ not run in the loop — needs an emulator |
| That test generator is not template-specific | discovers both generators' serialiser pairs | ✅ `fromSnapshot`/`toMap` and `fromDoc`/`toJson` |
| Claude output against a live Firestore | — | ❌ prompt warns of the trap, never run |
| Navigation between screens | — | ❌ still only "the screen builds" |
| Release build is unsigned, not debug-signed | real `--release` build, `apksigner` | ✅ verified unsigned |
| The artifact is signable by the buyer | signed with a throwaway keystore | ✅ verifies against their cert |
| Play upload | — | ❌ needs a Play account and a listing |

One caveat on the two Redis rows, since "✅" is doing real work there. There is
no Redis daemon on the development machine, so both were verified against
`fakeredis` — for the unit tests in-process, and for the cross-process durability
run over `TcpFakeServer`, a real TCP socket speaking real RESP. The process
boundaries, the sockets and the `SIGKILL` are genuine; the server implementing
`LMOVE` and key expiry is not. Running `scripts/verify_durable_execution.py`
against a real Redis is a `REDIS_URL` away and has not been done.

Those rows earned their asterisk the hard way. Everything above them is static —
`flutter analyze` proves the code type-checks, conformance proves it matches the
PRD, the generated widget tests prove each screen builds in isolation with its
providers overridden — and none of it runs the app. Actually running it found
two bugs in every app the generators had ever produced.

**The app could not start.** The composition root was `await
Firebase.initializeApp();` with no options, and the generator emits no
`google-services.json` either. On web that fails an assertion inside
`firebase_core_web` before the first frame; natively there is no config file to
fall back to. The app compiled, analysed clean, passed its widget tests and
packaged into a release-ready APK — and died on launch. Identifiers now come
from `--dart-define`, with `null` passed when none are supplied so a build
carrying a native config file still works.

**Dates threw on the way back in.** Firestore stores a `DateTime` as a
`Timestamp` and hands back a `Timestamp`, so `data['x'] as DateTime?` — well-typed
Dart — always threw the first time an app read a document it had written itself.

Both fixes are guarded by tests that fail against the old output and pass
against the new, which is the only reason to trust them.

That guard is no longer hand-written. The QA phase now emits an
`integration_test/<model>_roundtrip_test.dart` per model, and the emitted test
was itself checked the same way: it passes against the fixed mapper and fails
against the old one with the exact `Timestamp is not a subtype of DateTime?`.

It writes through the app's **own** serialiser and reads back through the app's
**own** deserialiser, which is the only formulation that is not a trap. A
hand-written document would have to pick a wire format, and the two generators
disagree on purpose — `TemplateGenerator` stores a date as a `Timestamp`, the
fixture generator as an ISO-8601 string, and both are correct because each reads
back what it wrote. Nothing about the pair is hard-coded either: the method
names are read out of the generated code, because `fromSnapshot`/`toMap` and
`fromDoc`/`toJson` are the same contract spelled differently, and a check
written against one generator's spelling silently stops testing the other.

Running it is opt-in. It needs an emulator and a browser, and making the
analysis loop depend on Node, a JDK 21 and Chrome would be a large tax on every
build for a check most builds cannot run.

Two limits on that ✅. It ran in **Chrome, not on Android** — this machine has no
AVD or system image — which is enough for a platform-independent Dart mapper bug
and not enough to claim the app works on a phone. And it covers the **template
generator only**: the Claude generator's prompt now documents the same trap, but
no Opus output has been put in front of a live Firestore.

251 unit tests; eval sweep 11/11 against real analysis and widget tests;
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
    roundtrip.py       generates the Firestore round-trip test from the output
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
  build/signing.py     unsigned release builds, and proving they are unsigned
  build/scaffold.py    generates android/ via `flutter create`
evals/
  prds/                10 PRDs chosen to break things
  run.py               pass-rate harness
emulator/              Firestore emulator config (a demo-* project; no keys)
```

## Notes and limitations

- **Escaping is load-bearing.** PRD text crosses into two generated languages,
  and each boundary needs its own escaping: `yaml_scalar` for `pubspec.yaml`,
  `dart_string` for Dart literals. Both exist because both broke in testing.
- **The Firestore emulator needs a different JDK than the build does.**
  `firebase-tools` refuses anything below JDK 21; the Android/Gradle toolchain
  here is pinned to 17. They coexist — the emulator is handed a 21+ on its own
  `PATH` and nothing else changes — but the failure reads as a Java problem
  rather than a version-policy one. Set `EMULATOR_JAVA_HOME` if the script's
  search does not find a 21+. It also needs `firebase-tools` and a
  `chromedriver` whose major version matches the installed Chrome.
- **The stub analyzer cannot parse Dart.** It enforces conventions and catches
  provider desync, but it passed a project with 59 syntax errors. Treat a green
  offline run as a smoke signal, not proof; use `--analyzer dart` for that.
- **Cupertino is a full template family**, not a swapped root widget — a
  `CupertinoApp` provides no `MaterialLocalizations`, so Material widgets throw
  at runtime inside one.
- **Smoke tests use stubbed providers.** They prove a screen builds and its data
  layer resolves; they do not prove navigation works or that writes reach
  Firestore.
