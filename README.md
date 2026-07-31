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
| `BUILDS_RATE_LIMIT` | `20` | `POST /builds` per address per window; `0` disables |
| `BUILDS_RATE_WINDOW_SECONDS` | `60` | the window that limit applies over |
| `BUILD_WORKER_EMBEDDED` | `1` | whether the API process also builds |
| `BUILD_LEASE_SECONDS` | `300` | how long a silent worker keeps its job |
| `BUILD_HEARTBEAT_SECONDS` | `30` | how often a working worker says so |
| `BUILD_MAX_ATTEMPTS` | `3` | before a build is given up on for good |

`/healthz` reports `durable_execution`, which is true only when the queue *and*
the job store are both shared — a Redis queue over an in-memory job store loses
the record the build would be resumed from.

### Why the accepting endpoint is throttled

The x402 gate stops anyone getting a *free* build, so this is not about theft. It
is the cost of **refusing**: `POST /builds` carrying a payment header makes the
service call the facilitator's `/verify` and then `/settle`, two network round
trips with a 60-second timeout, from a synchronous endpoint. A few hundred
concurrent requests with junk authorizations exhaust the thread pool and the
service stops answering anyone — including the buyers who paid. So the limit is
on that endpoint only; polling a running build and downloading an APK are cheap
and legitimately frequent, and throttling them would punish correct behaviour.

**Identifying the caller is the part that fails quietly.** Behind the TLS proxy
every request arrives from Caddy's address on the compose network, so keying on
the socket peer puts every buyer in the world into one bucket — a limiter that
looks configured while blocking either everyone or nobody. The client is the
**rightmost** `X-Forwarded-For` entry, because Caddy appends the peer it actually
saw to whatever the caller sent. Taking the leftmost, which is the more common
convention, would let a caller supply their own header and mint a fresh identity
per request; there is a test that fails on exactly that.

### Deploying it

One container that accepts payment and builds APKs, plus the Redis that makes
both durable. It wants a VM with a disk: the image carries Flutter, the Android
SDK and a JDK, and measures **7.71 GB**, which rules out serverless targets and
most PaaS free tiers.

A finished build used to cost **2.0 GB** of Gradle output for a **144 MB** APK,
and nothing reclaimed it — job records expire from Redis after seven days, which
is the wrong half, since the record is kilobytes and the directory it names is
gigabytes. The service now keeps the artifact and drops the tree the moment a
build finishes, while the worker still holds the lease. Measured on the
deployment: the last unpruned build is 2.0 GB on disk, the first pruned one is
144 MB, and it still downloads. That is the difference between about 58 sales
and about 790 on a 150 GB box.

```bash
cp .env.deploy.example .env.deploy     # fill in X402_PAY_TO and ANTHROPIC_API_KEY
docker compose --env-file .env.deploy up -d --build
poetry run python scripts/verify_deployment.py http://127.0.0.1:8000
```

`docs/DEPLOY.md` is the runbook: how the machine should be sized and why, the
firewall trap that Docker's published ports set for ufw, and the order — build,
prove over an SSH tunnel, buy one real build, and only then put it on a public
address behind TLS.

Every toolchain version in the `Dockerfile` is pinned to the one the table above
was proven against — Flutter 3.44.8, Android SDK 36, build-tools 36.0.0, JDK 17.
Floating any of them makes the build environment a moving target, which is the
one thing a service promising "it compiles" cannot afford.
`tests/test_deployment_config.py` fails if they drift, if a `${VAR}` in compose
has nothing to supply it, or if the builds volume disappears.

**The deployment check is the point of that last command.** A container can come
up, answer HTTP and still be unsellable: taking payments it never settles,
losing paid builds on restart, or falling back to the dev shared secret because
a token variable was misspelled. `/healthz` was built to make each of those
visible in one line, and `verify_deployment.py` refuses the deployment when it
sees them. It was itself checked against four deliberately broken deployments —
no Redis, no facilitator, no token config, wrong chain, no rate limit — and
caught all five
with the fix named in the message. `--pay` goes further and buys a real build
with the testnet key, which is the only check that proves the deployment can do
the thing it charges for.

Two things about the buyer-facing surface are worth knowing before pointing
traffic at it. The service defaults to the **Claude** generator, so a deployment
without `ANTHROPIC_API_KEY` accepts payment and then fails to build;
`SUPERVISOR_GENERATOR=template` is the free, deterministic path and a sane first
deploy. And `SUPERVISOR_BUILD_MODE` decides whether a buyer gets an installable
debug APK or an unsigned release one — see below for why the service will not
sign.

**The image has now been built and sold from.** A 4 vCPU / 8 GB Hetzner box
running Ubuntu 26.04 built it on the first attempt, and a signed EIP-3009
authorization settled on Base Sepolia
([`0x0637c94b…`](https://sepolia.basescan.org/tx/0x0637c94b490531065d3476147eaa7484ed2565ad8168b0639d21cf63b1edb2a5))
bought a 151 MB APK from it end to end.

It did not work first time, and the way it failed is the point. Every layer of
the image built, the service came up healthy, `/healthz` reported a correct
posture, and the deployment check passed — and then the first paid build died:

```
PermissionError: [Errno 13] Permission denied: '/opt/flutter/bin/flutter.bat'
```

The Flutter SDK ships `flutter` *and* `flutter.bat` on every platform. Four call
sites had independently resolved the tool by taking the first candidate that
`exists()`, Windows spelling first — correct here, an unrunnable batch file on
the machine that was actually going to run it. 299 tests, an eval sweep, APK
packaging and the entire verification table had all passed on Windows, where the
bug cannot appear. It took a buyer paying for a build to find it, which is
exactly the failure mode the paid check exists to provoke while the buyer is
still us. `src/ports/toolchain.py` now answers that question once, and takes
`windows=` as a parameter so both branches are reachable from a test on either
platform.

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
| Prove a user can tap their way into every screen | `poetry run python scripts/verify_navigation_flow.py` |
| Check a deployment is configured to sell builds | `poetry run python scripts/verify_deployment.py <url>` |
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
| Claude output against a live Firestore | Opus 5 app, emulator + Chrome | ✅ round trip passes |
| Discovery works on model code nobody wrote for it | Opus, Haiku, template, fixture | ✅ four serialiser shapes |
| Declared navigation is actually wired | conformance `unreachable_screen` | ✅ found 3 dead routes |
| The destination renders when navigated to | app's own `main()` + `Navigator`, Chrome | ✅ generated per PRD |
| A user tapping the affordance arrives | the app's own buttons, Chrome + emulator | ✅ template output, 3 actions |
| Those tap tests are not decorative | affordance hidden, everything else intact | ✅ exactly the right test went red |
| The same against a model's own widget choices | — | ❌ not yet run on Claude output |
| A tap arriving after a form is filled in | — | ❌ multi-step flows; see below |
| Release build is unsigned, not debug-signed | real `--release` build, `apksigner` | ✅ verified unsigned |
| The artifact is signable by the buyer | signed with a throwaway keystore | ✅ verifies against their cert |
| Play upload | — | ❌ needs a Play account and a listing |
| A misconfigured deployment is refused | 5 broken services, real HTTP | ✅ caught all 5, fix named |
| The accepting endpoint is bounded | limiter + endpoint tests | ✅ 21 tests, both backends |
| The suite runs on Linux, not just Windows | GitHub Actions matrix | ⚠️ written, never executed — no remote |
| The deployment image builds | Hetzner VM, Ubuntu 26.04, amd64 | ✅ 7.71 GB, first attempt |
| A buyer pays and receives an APK | live deployment, Base Sepolia | ✅ real tx, 151 MB APK |
| The toolchain resolves on Linux | the deployed build | ✅ was broken; see below |
| A finished build stops costing 2 GB | live deployment, before/after | ✅ 2.0 GB → 144 MB |
| The pruned build still downloads | re-fetched after pruning | ✅ `200`, 151 MB |
| Reachable over TLS on a public address | Let's Encrypt, TLS-ALPN-01 | ✅ trusted cert, no tunnel |
| A buyer on the open internet can buy | paid run over public HTTPS | ✅ 151 MB APK delivered |

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

One limit on that ✅: it ran in **Chrome, not on Android** — this machine has no
AVD or system image — which is enough for a platform-independent Dart mapper bug
and not enough to claim the app works on a phone.

Opus output has now been through it, and passed: the model it wrote handles
`Timestamp` correctly in both directions, so the prompt guidance holds up rather
than merely existing. Getting there cost two bugs, both in the *discovery* and
both the same mistake this module was written to avoid — over-fitting to the one
generator that was available while writing it.

- `TemplateGenerator` marks every constructor parameter `required`. Claude makes
  most of them optional with defaults. Matching only `required this.x` found
  nothing to assert and emitted **no test at all** — failure that looks
  identical to success.
- The deserialiser need not take a `DocumentSnapshot`. Opus writes
  `fromMap(String id, Map<String, dynamic> map)`, keeping the model free of
  Firestore types, which is arguably the better design and matched nothing.

Four serialiser shapes are now covered — `fromSnapshot`/`toMap`,
`fromDoc`/`toJson`, `fromFirestore`/`toFirestore`, `fromMap`/`toMap` — across
two call arities. The lesson is the one this repo keeps relearning: a check
written while looking at a single generator encodes that generator's taste, and
the only way to find out is to run it against code somebody else wrote.

### The routes nothing could reach

The PRD declares navigation as data — `Action(kind="navigate", target=<screen>)`
— and the schema already refuses targets that name screens which do not exist.
The generator listed every one of those screens in the routes table and then
never pushed a single route. `/capture` and `/settings` existed and no widget
could get to them: the app was a form nobody could open.

Everything passed. Each screen built in isolation, so the smoke tests were
green. The routes table was valid Dart, and the analyzer has no opinion about
whether a route is ever navigated to. Conformance checked that the screens
existed, which they did.

`unreachable_screen` closes it, and is deliberately loose about *how* a screen is
reached — a named route, a `MaterialPageRoute` building the widget, or a
router's `go()` all count. What it will not accept is the target appearing only
in the routes table that declares it, which is exactly the shape the bug had.

Two things worth recording. The first version of the check was **vacuous**: it
searched every UI file for `CaptureScreen(` and found the destination's own
constructor in its own file, so every unreachable screen looked reachable. It
was caught by running it against the actual broken output rather than a fixture.
And once it worked it immediately found two more instances nobody had looked
for — `auth_flow` and `many_screens` — because the fix had only covered list
screens, and auth, settings and detail screens had no affordance to hang
navigation off at all.

`unreachable_screen` is a **static** guarantee: something in the app navigates to
every declared target. It says nothing about whether the destination works, and
it cannot — `'/capture': (context) => const CaptureScreen()` satisfies it whether
or not `CaptureScreen` survives being built.

So the QA phase also generates `integration_test/navigation_test.dart`, which
boots the app through its **own `main()`** — the real composition root, the real
`ProviderScope`, the real Firebase initialisation — drives its own `Navigator` to
each declared target, and checks the destination is on screen. Delete a route
from the generated app and it fails with `Could not find a generator for route
RouteSettings("/capture")`, which is how we know it is testing anything.

Destinations are identified by their **PRD title**, the one label that is the
same whatever a generator calls its classes, files or routes. Matching
`<Id>Screen` would be matching one generator's convention.

### Tapping the button, and how we know that test is real

Driving the navigator still leaves the user out of it. `unreachable_screen` says
*something in the source* navigates to each target; the push case says the
destination renders once you get there. Neither says a person looking at the
source screen has anything to tap.

The first version of this refused to go further, on the grounds that hunting for
a button depends on widget choices and would go flaky. Three decisions make it
honest instead:

* **What counts as tappable is not a list of buttons.** `InkResponse` sits under
  `TextButton`, `ElevatedButton`, `IconButton`, `FloatingActionButton` and
  `ListTile`; `CupertinoButton` and a hand-rolled `GestureDetector` cover the
  rest. Enumerating button classes would have encoded `TemplateGenerator`'s taste
  — and would have missed the affordance that ships most, since a generated list
  screen navigates from a FAB whose only child is an icon and whose label is
  therefore nothing at all.
* **Every attempt restarts from a freshly pushed source screen**, which turns
  "tap things until something happens" from one stateful sequence into N
  independent one-tap experiments. It matters here: the generated FAB writes a
  draft document before it navigates, so without the reset each attempt would
  face a different screen than the last.
* **Arrival is the route name first and the title second.** A button labelled
  with the destination's title is good UI, and if arrival meant "the title is on
  screen" the test would pass before tapping anything — so the title only counts
  when it was *not* already visible.

None of that is evidence. `scripts/verify_navigation_flow.py` is: it generates
the eight-screen `many_screens` app, runs the navigation test in Chrome against
a Firestore emulator, and then runs it again against a mutant with one
affordance wrapped in `Offstage(offstage: true, ...)`.

That mutation is chosen for what it leaves working. The `Navigator.pushNamed`
call is still in the file, so `unreachable_screen` still passes. The route table
is untouched, so the push case still passes. The widget is still in the tree, so
nothing about the build changes. It is simply never on screen — the app ships
with a screen no user can open, and everything static stays green:

```
Failure in method: openToday: a tap on inbox reaches today
  Expected: empty
    Actual: 'tapped 2 of 2 affordance(s) on /inbox and none of them reached /today'
```

Exactly one of the six generated cases went red, and it was the right one. That
is the only reason to believe the tap cases test anything.

**What it still cannot prove** is that a *multi-step* flow arrives. A sign-in
screen that navigates only after valid credentials needs a tap and some typing,
and the PRD says nothing about what to type. Those fail here rather than being
quietly skipped, which is the right direction — but it means a red tap case is
"look at this", not automatically "the app is broken".

**And it has only been run on template output.** Every over-fitting bug in this
repo was invisible until code somebody else wrote went through the check — the
round-trip discovery was broken twice that way. The affordance search is written
not to care whose widgets it is looking at, which is exactly the kind of claim
that has been wrong here before. `--generator claude` on this script is the next
thing worth spending on.

**Automation needs its own browser.** `flutter drive -d chrome` cannot get a
controllable Chrome while an ordinary Chrome session is running — the launch is
delegated to the existing instance, the debug port is never opened, and `dwds`
fails with `AppConnectionException` after about 25 seconds. It reads like a code
fault and is not one. Point `CHROME_EXECUTABLE` at a Chrome-for-Testing binary
whose version matches `chromedriver`:

```bash
npx @puppeteer/browsers install chrome@150.0.7871.124 --path ~/chrome-for-testing
export CHROME_EXECUTABLE=~/chrome-for-testing/chrome/win64-150.0.7871.124/chrome-win64/chrome.exe
```

`flutter drive` also wants the WebDriver on port 4444 unless `--driver-port`
says otherwise, and the failure it gives for a chromedriver on any other port
names 4444 rather than the port it was asked for.

339 unit tests; eval sweep 11/11 against real analysis and widget tests;
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
    navigation.py      generates the runtime navigation test: push and tap
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
