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

## Commands

| What | Command |
| --- | --- |
| Build one app | `poetry run python src/supervisor.py <prd.json>` |
| Eval sweep (offline, ~1s) | `poetry run python evals/run.py` |
| Eval sweep (real analysis) | `poetry run python evals/run.py --analyzer dart --flutter-root "C:\flutter"` |
| Eval sweep (+ widget tests) | add `--run-tests` |
| Unit tests | `poetry run pytest` |

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

## The two generators

Both implement the same `CodeGenerator` port, so the graph does not know which
it has.

- **`template`** (default) — deterministic Python templates. No network, no
  credentials. This is the validated path.
- **`fixture`** — a test double emitting correct Flutter that shares none of
  `TemplateGenerator`'s naming conventions. Proves the grading harness is not
  merely grading its own output. See `evals/fixture_generator.py`.
- **`claude`** — real Claude calls with structured outputs.
  **Never executed.** See the warning at the top of `src/ports/llm.py`.

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
| Claude generator — request shape | fake transport, 17 tests | ✅ |
| Harness works on non-template code | `--generator fixture` | ✅ full pipeline + APK path |
| **Claude generator — live API** | — | ❌ never run |
| APK packaging | `flutter build apk --debug` | ✅ 144 MB APK, correct applicationId |
| Navigation / real Firestore I/O | — | ❌ needs an emulator |
| Release signing / Play upload | — | ❌ debug keystore only |

109 unit tests; eval sweep 11/11 against real analysis and widget tests;
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
    llm.py             Claude generator (UNVALIDATED)
    analyzer.py        StubAnalyzer (offline) + FlutterAnalyzer (real)
    conformance.py     does the app match the PRD?
    smoke.py           generates widget tests from the output
    runtime.py         runs them
    ownership.py       which agent can fix which diagnostic
  payments/x402.py     the payment gate
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
