"""Generated tests that prove a model survives a trip through Firestore.

Everything else the pipeline checks is static, and static analysis is blind to
the failure that actually shipped: a generated model that casts a Firestore
`Timestamp` straight to `DateTime`. That type-checks, passes the widget smoke
tests, and throws the first time the app reads back a document it wrote.

So this generates, per model, a test that writes through the app's **own**
serializer and reads back through the app's **own** deserializer, then checks
the fields came back unchanged.

**Why that formulation and not a nicer-looking one.** The obvious test — write a
document by hand, read it with the generated mapper — is wrong, and wrong in the
way this codebase keeps getting wrong. `TemplateGenerator` stores a date as a
`Timestamp`; the fixture generator stores it as an ISO-8601 string. Both are
correct, because each reads back what it wrote. A hand-written document has to
pick one convention, and would fail the other generator for having different
taste rather than a bug. Round-tripping through the pair is the only check that
holds a generator to its *own* contract, and it is exactly the property the
shipped bug violated.

For the same reason nothing here is hard-coded. The serializer pair is
discovered: `TemplateGenerator` writes `fromSnapshot`/`toMap` into
`lib/providers/`, the fixture generator writes `fromDoc`/`toJson` into
`lib/models/`, and a test written against either set of names would silently
stop testing the other. Over-fitting checks to one generator's conventions is
the single most repeated mistake in this repo's history — 20 of 22 eval failures
at one point — so the names are read out of the generated code.

When the pair cannot be found, no test is emitted for that model. A missing test
is a visible gap; a test that fails because the generator named things
differently is a false alarm that trains everyone to ignore this file.
"""

from __future__ import annotations

import re

from src.ports.templates import pascal, snake
from src.prd.schema import PRD

# `factory Observation.fromSnapshot(DocumentSnapshot<Map<String, dynamic>> doc)`
# and `factory Observation.fromDoc(DocumentSnapshot<...> doc)`.
_FROM_SNAPSHOT = re.compile(
    r"factory\s+(\w+)\s*\.\s*(\w+)\s*\(\s*DocumentSnapshot", re.M
)

# `Map<String, dynamic> toMap() {` and `Map<String, dynamic> toJson() =>`.
# The empty parens matter: they keep this from matching the `DocumentSnapshot<
# Map<String, dynamic>> doc` in the factory signature above.
_TO_MAP = re.compile(r"Map<\s*String\s*,\s*dynamic\s*>\s+(\w+)\s*\(\s*\)")

_REQUIRED_PARAM = re.compile(r"required\s+this\.(\w+)")
_FIELD_DECL = re.compile(r"final\s+([\w<>?]+)\s+(\w+)\s*;")
_PUBSPEC_NAME = re.compile(r"^name:\s*([A-Za-z_][A-Za-z0-9_]*)", re.M)


class ModelApi:
    """The serialiser pair for one model, as found in the generated code."""

    def __init__(
        self,
        cls: str,
        path: str,
        from_snapshot: str,
        to_map: str,
        required: list[str],
        types: dict[str, str],
    ) -> None:
        self.cls = cls
        self.path = path
        self.from_snapshot = from_snapshot
        self.to_map = to_map
        self.required = required
        self.types = types


def _package_name(prd: PRD, files: dict[str, str]) -> str:
    """The Dart package name, from the manifest rather than from the PRD.

    Same reasoning as `smoke.py`: deriving it from `app_name` assumes one
    generator's naming and breaks every `package:` import for any other.
    """
    match = _PUBSPEC_NAME.search(files.get("pubspec.yaml", ""))
    return match.group(1) if match else snake(prd.app_name)


def _find_model_api(files: dict[str, str], model_name: str) -> ModelApi | None:
    """Locate the class that serialises `model_name`, whatever it is called."""
    expected = pascal(model_name)
    candidates: list[ModelApi] = []

    for path, body in sorted(files.items()):
        if not path.startswith("lib/"):
            continue
        for cls, from_snapshot in _FROM_SNAPSHOT.findall(body):
            to_map = _TO_MAP.search(body)
            if to_map is None:
                # It can read documents but not write them; a round trip needs
                # both halves, and inventing the missing one is guesswork.
                continue
            candidates.append(
                ModelApi(
                    cls=cls,
                    path=path,
                    from_snapshot=from_snapshot,
                    to_map=to_map.group(1),
                    required=_REQUIRED_PARAM.findall(body),
                    types=dict(
                        (name, dart_type)
                        for dart_type, name in _FIELD_DECL.findall(body)
                    ),
                )
            )

    for candidate in candidates:
        if candidate.cls == expected:
            return candidate
    # Exactly one serialisable class in the whole app is unambiguous even if it
    # is spelled differently. More than one, and picking would be a guess.
    return candidates[0] if len(candidates) == 1 else None


def _sample_literal(field_type: str) -> str:
    """A Dart literal for a PRD field type, used as the value written."""
    return {
        "text": "'round trip'",
        "number": "7",
        "bool": "true",
        "date": "stamp",
    }[field_type]


def _placeholder(dart_type: str) -> str:
    """A value for a constructor parameter the PRD does not describe.

    `id` is the usual one: generated models carry `doc.id`, which Firestore
    assigns on write, so whatever goes in here is discarded. It only has to
    compile — and it is never asserted on.
    """
    base = dart_type.rstrip("?")
    return {
        "String": "'placeholder'",
        "num": "0",
        "int": "0",
        "double": "0",
        "bool": "false",
        "DateTime": "stamp",
    }.get(base, "null")


def build_roundtrip_tests(prd: PRD, files: dict[str, str]) -> dict[str, str]:
    """Return `integration_test/*_roundtrip_test.dart` for every model we can test."""
    package = _package_name(prd, files)
    tests: dict[str, str] = {}

    for model in prd.models:
        api = _find_model_api(files, model.name)
        if api is None:
            continue

        fields = {f.name: f.type for f in model.fields}
        arguments = []
        for name in api.required:
            if name in fields:
                arguments.append(f"      {name}: {_sample_literal(fields[name])},")
            else:
                arguments.append(f"      {name}: {_placeholder(api.types.get(name, ''))},")

        # Only PRD fields are asserted. `id` is reassigned by Firestore, and any
        # other constructor parameter is the generator's business, not ours.
        checks = "\n".join(
            f"    expect(restored.{name}, original.{name},\n"
            f"        reason: '{name} did not survive the round trip');"
            for name in api.required
            if name in fields
        )
        if not checks:
            continue

        # Only declare the clock if something actually uses it. A model with no
        # date field would otherwise get `unused_local_variable`, which the
        # analyzer treats as an error and which fails the whole build.
        uses_stamp = any("stamp" in argument for argument in arguments)
        stamp_decl = (
            """    // Whole seconds. Firestore keeps sub-millisecond precision and a
    // generator storing dates as ISO strings keeps microseconds, so comparing
    // finer than this would make the test about rounding, not correctness.
    final stamp = DateTime.fromMillisecondsSinceEpoch(
      (DateTime.now().millisecondsSinceEpoch ~/ 1000) * 1000,
    );

"""
            if uses_stamp
            else ""
        )

        import_path = api.path[len("lib/") :]
        tests[f"integration_test/{snake(model.name)}_roundtrip_test.dart"] = f"""// Generated. Proves {api.cls} survives Firestore, using the app's own
// serialiser pair ({api.to_map} / {api.from_snapshot}) rather than a
// hand-written document, so a generator is held to its own convention.
// The framework imports are prefixed and the app's is not, so the model name
// always wins. A PRD is free to call a model `Order`, `Query` or `Source`, and
// cloud_firestore exports all three — an unprefixed import makes that an
// ambiguous_import error, which is a build failure caused purely by the buyer's
// choice of noun.
import 'package:cloud_firestore/cloud_firestore.dart' as fs;
import 'package:firebase_core/firebase_core.dart' as core;
import 'package:flutter/widgets.dart' as widgets;
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

import 'package:{package}/{import_path}';

const _emulator = String.fromEnvironment('FIRESTORE_EMULATOR');

void main() {{
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() async {{
    await core.Firebase.initializeApp(
      options: const core.FirebaseOptions(
        apiKey: 'demo-key',
        appId: '1:1:web:demo',
        projectId: 'demo-roundtrip',
        messagingSenderId: '1',
      ),
    );
    final separator = _emulator.lastIndexOf(':');
    fs.FirebaseFirestore.instance.useFirestoreEmulator(
      _emulator.substring(0, separator),
      int.parse(_emulator.substring(separator + 1)),
    );
  }});

  testWidgets('{api.cls} survives a Firestore round trip', (tester) async {{
    // Gives the binding a view. Without one the gesture layer throws
    // "Bad state: No element" on every pointer packet the browser delivers.
    await tester.pumpWidget(const widgets.SizedBox.shrink());

{stamp_decl}    final original = {api.cls}(
{chr(10).join(arguments)}
    );

    final reference = await fs.FirebaseFirestore.instance
        .collection('{model.collection}')
        .add(original.{api.to_map}());
    final restored = {api.cls}.{api.from_snapshot}(await reference.get());

{checks}
  }});
}}
"""

    if tests:
        # `flutter drive` needs a driver entry point, and an app that ships
        # integration tests it cannot run is only half a deliverable. Emitted
        # only alongside a test, so an app with nothing to round-trip does not
        # carry a driver for tests that do not exist.
        tests["test_driver/integration_test.dart"] = (
            "// Generated. Entry point for `flutter drive`.\n"
            "import 'package:integration_test/integration_test_driver.dart';\n"
            "\n"
            "Future<void> main() => integrationDriver();\n"
        )
    return tests
