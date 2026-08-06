# PRD to Flutter APK

Give it a JSON description of an app. Get back Flutter source that compiles —
and, if you want it, an installable Android APK.

**Generating and reading the source is free.** You only pay for the packaged
APK. That way you can see exactly what it writes for *your* spec before deciding
whether it's worth anything to you.

## What it actually does

Four stages, each one handing off to the next:

1. **Plan** — turns your spec into a design and a `pubspec.yaml`
2. **UI** — writes the widget tree
3. **State** — wires Riverpod providers, models and Firestore access into it
4. **QA** — runs `flutter analyze` and the generated widget tests

When the analyser finds something, the diagnostic goes back to whichever stage
owns that file — a broken widget to the UI stage, a missing provider to the
state stage — and that stage gets another pass. Up to three rounds, then it
stops and hands you the diagnostics rather than pretending it worked.

That loop is the whole point. "The model wrote Dart" and "the Dart compiles"
are different claims, and only the second one is worth paying for.

## Input

```json
{
  "app_name": "Field Notes",
  "package_name": "com.example.fieldnotes",
  "theme": "material",
  "auth": true,
  "models": [
    {
      "name": "Observation",
      "collection": "observations",
      "fields": [
        { "name": "title", "label": "Title", "type": "text" },
        { "name": "count", "label": "Specimen count", "type": "number" }
      ]
    }
  ],
  "screens": [
    {
      "id": "observations",
      "title": "Observations",
      "kind": "list",
      "model": "Observation",
      "actions": [{ "name": "openCapture", "kind": "navigate", "target": "capture" }]
    },
    {
      "id": "capture",
      "title": "New observation",
      "kind": "form",
      "model": "Observation",
      "actions": [{ "name": "saveObservation", "kind": "create", "target": "Observation" }]
    }
  ]
}
```

`app_name`, `package_name` (reverse-DNS) and `screens` are required. Screens can
be `list`, `form`, `detail`, `auth` or `settings`. Actions can navigate, create,
update, delete, or sign in and out.

## Output

Every generated file lands in the dataset — one record per file, with its full
contents — so you can read the code directly in the console.

`OUTPUT` in the key-value store summarises the run:

```json
{
  "app_name": "Field Notes",
  "files_generated": 20,
  "diagnostics": [],
  "clean": true,
  "packaged": true,
  "apk": "https://api.apify.com/v2/key-value-stores/.../app.apk?signature=..."
}
```

`clean: true` means `flutter analyze` had nothing to say. If it isn't clean, the
diagnostics are listed — a build that failed tells you why rather than failing
silently.

With **Package an APK** enabled you also get `app.apk` in the key-value store,
built by Gradle. A recent run produced a 151 MB debug APK.

## The model

Leave **Anthropic API key** empty and it uses a built-in template generator: the
output compiles and the structure is real, but it isn't model-designed.

Supply your own key and the four stages are driven by Claude, which is what the
example above came from — it produced a shared widget for loading and error
states, a Firestore query scoped to the signed-in user, friendly messages mapped
from Firestore error codes, and a derived provider totalling a numeric field that
the input spec never asked for.

Your key is used for that run and not stored.

## What you don't get

- **No `firebase_options.dart`.** It holds project credentials, so it is never
  generated. The app reads its Firebase config from `--dart-define` values at
  build time and degrades rather than crashing when none are supplied.
- **No signed release build.** The APK is a debug artifact. Signing needs your
  keystore, which is yours.
- **Not a replacement for writing the app.** It is a scaffold that compiles and
  a starting point that isn't a blank page.

## Source

MIT licensed: [github.com/carlosge492/app-generation-microservice](https://github.com/carlosge492/app-generation-microservice)

A complete, unmodified example of the output — 23 generated files from a
45-line spec, plus a short note describing the run — is checked into
[`examples/generated-field-notes/`](https://github.com/carlosge492/app-generation-microservice/tree/master/examples/generated-field-notes)
if you'd rather read the code than take any of this on trust.
