# What $3 actually produces

This directory is **unmodified output** from the service. Nothing here was
hand-written, hand-fixed, or cleaned up for presentation. It is checked in so
you can read exactly what you would get before deciding whether to pay for a
build of your own.

The input was [`examples/todo_app.prd.json`](../todo_app.prd.json) — a 45-line
JSON spec describing three screens and one data model. The output is the
23 files beside this one.

## The run that produced it

```
planning: DESIGN.md (15,972 chars) + pubspec.yaml drafted
genui:    8 widget file(s) written
logic:    5 state file(s) written
qa:       23 file(s) analysed (5 smoke tests, 1 navigation test),
          0 error(s), 0 non-fatal
```

Zero diagnostics on the first pass, so the repair loop never ran. That is not
the guaranteed case — the loop exists because it often does — but it is what
happened here, and `flutter analyze` is what decided it, not a self-assessment.

## Worth reading first

- [`lib/ui/observations_screen.dart`](lib/ui/observations_screen.dart) — the
  list screen. Note the empty state, and that the loading and error branches
  are delegated to a shared widget rather than repeated per screen.
- [`lib/providers/observation_providers.dart`](lib/providers/observation_providers.dart)
  — the most interesting file. `specimenTotalProvider` derives a running total
  across observations; the PRD never asked for it, but the spec has a `count`
  field and a total was inferred as worth surfacing. The query is scoped per
  user (`where('ownerId', isEqualTo: uid)`) rather than reading the whole
  collection, Firestore error codes are mapped to messages a person can act on
  instead of surfacing `permission-denied` raw, and every state write is guarded
  by a `mounted` check. None of that is in the input document.
- [`lib/ui/widgets/async_value_view.dart`](lib/ui/widgets/async_value_view.dart)
  — one place that handles loading, error, and retry for every async screen.
- [`test/`](test) — smoke tests per screen, generated alongside the code.

## What is not here

No `android/` or `ios/` directory: those are scaffolded at packaging time, which
only runs after payment settles. No `firebase_options.dart` either — that file
carries project credentials, so it is deliberately never generated. The app
reads its Firebase config from `--dart-define` values at build time instead, and
degrades rather than crashing when none are supplied.

This is a debug-mode build target. The service can produce release APKs, in
which case it verifies the artifact is unsigned rather than trusting that it is.
