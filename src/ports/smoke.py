"""Generate widget smoke tests for the produced app.

Everything else in this pipeline grades source code without executing it. A
screen can analyse clean, satisfy every conformance check, and still throw the
instant it is built — a missing ProviderScope, a provider that hits Firestore at
construction, or a Material widget inside a CupertinoApp (no MaterialLocalizations)
are all invisible to static analysis.

These tests are derived from the generated sources, not from the PRD: class
names, file names and provider names are *discovered* by scanning the output. A
generator that names things differently from `TemplateGenerator` still gets
valid tests, which matters because the point is eventually to grade the model.

Only the StreamProviders a screen actually watches are overridden, and only the
files it actually needs are imported — `unused_import` is escalated to an error,
and `flutter analyze` reads test/ too, so a sloppy test file would fail the build
it is meant to validate.
"""

from __future__ import annotations

import re

from src.ports.templates import snake
from src.prd.schema import PRD

# `class ObservationsScreen extends ConsumerWidget {`
_WIDGET_CLASS = re.compile(
    r"^class\s+([A-Za-z]\w*)\s+extends\s+"
    r"(?:ConsumerWidget|StatelessWidget|ConsumerStatefulWidget|StatefulWidget)\b",
    re.M,
)
# `final observationListProvider = StreamProvider<List<Observation>>((ref) {`
# and the qualified forms a generator may equally well pick:
#   StreamProvider.autoDispose<List<T>>   FutureProvider<List<T>>
#
# The qualifier chain is not cosmetic. An unmatched declaration means no
# override is emitted, the screen reaches real Firestore, and the generated
# `.when(error: ...)` handler renders an error widget instead of throwing — so
# the smoke test passes while asserting nothing. A test that cannot fail is
# worse than no test, because it reports as coverage.
_ASYNC_PROVIDER = re.compile(
    r"^final\s+(\w+)\s*=\s*(Stream|Future)Provider(?:\.\w+)*\s*<\s*List<\s*(\w+)\s*>\s*>",
    re.M,
)
_PROVIDER_REF = re.compile(r"\bref\.(?:watch|read)\(\s*([a-zA-Z_]\w*)\s*[.)]")

TEST_HEADER = "// GENERATED — asserts the screen builds without throwing.\n"


_PUBSPEC_NAME = re.compile(r"^name:\s*['\"]?([a-z_][a-z0-9_]*)['\"]?\s*$", re.M)


def _package_name(prd: PRD, files: dict[str, str]) -> str:
    """The Dart package name, read from the manifest that will actually be used.

    Deriving it from the PRD assumed TemplateGenerator's convention, where the
    pubspec is named `snake(app_name)`. A generator that picks any other name
    makes every `package:<pkg>/...` import in these tests point at a package
    that does not exist, and the whole test file fails to resolve — which reads
    as broken generated code rather than a broken test generator.
    """
    match = _PUBSPEC_NAME.search(files.get("pubspec.yaml", ""))
    return match.group(1) if match else snake(prd.app_name)


def _needs_arguments(body: str, cls: str) -> bool:
    """Does constructing `cls` require passing anything in?"""
    match = re.search(rf"(?:const\s+)?{re.escape(cls)}\s*\(([^)]*)\)", body)
    if match is None:
        return False
    params = match.group(1)
    if "required" in params:
        return True
    # Anything left once the optional named block `{...}` is removed is a
    # required positional parameter.
    positional = re.sub(r"\{[^}]*\}", "", params).strip().strip(",").strip()
    return bool(positional)


def _discover_async_providers(files: dict[str, str]) -> dict[str, tuple[str, str, str]]:
    """provider name -> (kind, element type, declaring file)."""
    found: dict[str, tuple[str, str, str]] = {}
    for path, body in files.items():
        if not path.startswith("lib/providers/"):
            continue
        for name, kind, element in _ASYNC_PROVIDER.findall(body):
            found[name] = (kind, element, path)
    return found


def build_smoke_tests(prd: PRD, files: dict[str, str]) -> dict[str, str]:
    """Return test/**_smoke_test.dart for every generated screen widget."""
    pkg = _package_name(prd, files)
    framework = "cupertino" if prd.theme == "cupertino" else "material"
    app_widget = "CupertinoApp" if prd.theme == "cupertino" else "MaterialApp"
    streams = _discover_async_providers(files)

    tests: dict[str, str] = {}
    for path, body in sorted(files.items()):
        if not path.startswith("lib/ui/") or path.endswith("/app.dart"):
            continue
        classes = _WIDGET_CLASS.findall(body)
        if not classes:
            continue
        widget = classes[0]

        # Only widgets that can stand alone. A well-factored generator extracts
        # reusable components — `ArticleTile({required this.article})`,
        # `ErrorView({required this.message})` — and pumping those with no
        # arguments produces `missing_required_argument` against code that is
        # perfectly correct. A screen is something the app can show by itself;
        # if it needs data passed in, it is a component and not ours to smoke.
        if _needs_arguments(body, widget):
            continue

        # Only the providers this screen touches, so no import is unused.
        watched = [n for n in dict.fromkeys(_PROVIDER_REF.findall(body)) if n in streams]
        provider_files = sorted({streams[n][2] for n in watched})

        imports = [
            f"import 'package:flutter/{framework}.dart';",
            "import 'package:flutter_riverpod/flutter_riverpod.dart';",
            "import 'package:flutter_test/flutter_test.dart';",
            "",
            f"import 'package:{pkg}/{path[len('lib/'):]}';",
        ]
        imports += [f"import 'package:{pkg}/{p[len('lib/'):]}';" for p in provider_files]

        if watched:
            overrides = "\n".join(
                # [0] is the kind (Stream/Future), [1] the element type.
                f"        {name}.overrideWith(\n"
                f"          (ref) => {streams[name][0]}<List<{streams[name][1]}>>"
                f".value(<{streams[name][1]}>[]),\n"
                f"        ),"
                for name in watched
            )
        else:
            overrides = "        // no data providers on this screen"

        # Asserting only `takeException() == null` is too weak: the generated
        # screens handle provider failures with `.when(error: ...)`, which
        # renders an error widget and swallows the exception, so a screen whose
        # data layer is entirely broken still passes. Reading the provider's
        # future asserts the data actually resolves.
        resolves = "\n".join(
            f"    await expectLater(container.read({name}.future), completes);"
            for name in watched
        )

        tests[f"test/{snake(widget)}_smoke_test.dart"] = TEST_HEADER + "\n".join(imports) + f"""

void main() {{
  testWidgets('{widget} builds without throwing', (WidgetTester tester) async {{
    final container = ProviderContainer(
      overrides: [
{overrides}
      ],
    );
    addTearDown(container.dispose);

    await tester.pumpWidget(
      UncontrolledProviderScope(
        container: container,
        child: const {app_widget}(home: {widget}()),
      ),
    );
    await tester.pump();

    expect(tester.takeException(), isNull);
    expect(find.byType({widget}), findsOneWidget);
{resolves}
  }});
}}
"""
    return tests
