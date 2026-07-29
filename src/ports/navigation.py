"""Generated tests that walk the app's declared navigation at runtime.

`conformance.py` proves *something* navigates to every screen the PRD says is a
target. That is a static claim about text in a file. It does not prove the
destination renders, and it cannot: the route table entry
`'/capture': (context) => const CaptureScreen()` satisfies the static check
whether or not `CaptureScreen` survives being built for real.

This closes the other half. It boots the app through its own `main()` — the real
composition root, the real `ProviderScope`, the real Firebase initialisation —
then drives the app's own `Navigator` to each declared target and checks the
destination is on screen. Two things that were separately broken get exercised
in passing: an app whose `main()` could not start, and screens that had only
ever been built in isolation by the smoke tests.

**Why the destination is identified by its title.** Screen titles come from the
PRD, so they are the one label guaranteed to be the same whatever a generator
names its classes, files or routes. Matching a widget class would be matching
`TemplateGenerator`'s `<Id>Screen` convention, and Claude does not always use it.

**Why the route name is not taken from the generator either.** The route is
`'/<screen id>'`, derived from the PRD, and both generators independently chose
that shape. Where a generator does something else the test fails loudly rather
than silently passing, which is the right direction for a check whose whole
purpose is catching unreachable screens.

**What it deliberately does not do** is hunt for the button. Tapping "whatever
looks tappable" until the destination appears is possible, but it depends on
widget choices that vary far more than a title does, and a flaky navigation test
is worse than an honest static one. Reachability-by-tap stays with
`unreachable_screen`; this proves the destination works once reached.
"""

from __future__ import annotations

import re

from src.ports.templates import snake
from src.prd.schema import PRD

_PUBSPEC_NAME = re.compile(r"^name:\s*([A-Za-z_][A-Za-z0-9_]*)", re.M)


def _package_name(prd: PRD, files: dict[str, str]) -> str:
    match = _PUBSPEC_NAME.search(files.get("pubspec.yaml", ""))
    return match.group(1) if match else snake(prd.app_name)


def _dart_literal(text: str) -> str:
    """A single-quoted Dart string. Titles are free text from the PRD."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("$", "\\$")
        .replace("\r", "")
        .replace("\n", "\\n")
    )
    return f"'{escaped}'"


def build_navigation_test(prd: PRD, files: dict[str, str]) -> dict[str, str]:
    """Return `integration_test/navigation_test.dart`, or nothing to test."""
    titles = {screen.id: screen.title for screen in prd.screens}
    cases: list[str] = []

    for screen in prd.screens:
        for action in screen.actions:
            if action.kind != "navigate" or not action.target:
                continue
            destination = titles.get(action.target)
            if destination is None:
                # The schema rejects unknown targets, so this is unreachable;
                # skipping beats emitting a test that cannot compile.
                continue
            cases.append(f"""
  testWidgets('{action.name}: {screen.id} -> {action.target}', (tester) async {{
    await app.main();
    await tester.pumpAndSettle();

    final navigator = tester.state<widgets.NavigatorState>(
      find.byType(widgets.Navigator).first,
    );
    navigator.pushNamed('/{action.target}');
    await tester.pumpAndSettle();

    expect(
      find.text({_dart_literal(destination)}),
      findsWidgets,
      reason: 'pushing /{action.target} did not show the {action.target} screen; '
          'the route is declared but the destination does not render',
    );
  }});""")

    if not cases:
        return {}

    package = _package_name(prd, files)
    body = "\n".join(cases)
    return {
        # Identical to the one `roundtrip.py` writes, and emitted here too so a
        # PRD with navigation but no data model still ships a runnable driver.
        "test_driver/integration_test.dart": (
            "// Generated. Entry point for `flutter drive`.\n"
            "import 'package:integration_test/integration_test_driver.dart';\n"
            "\n"
            "Future<void> main() => integrationDriver();\n"
        ),
        "integration_test/navigation_test.dart": f"""// Generated. Boots the app through its own main() and drives its Navigator to
// every screen the PRD declares as a navigate target, checking the destination
// actually renders — which the routes table alone never proved.
import 'package:flutter/widgets.dart' as widgets;
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

import 'package:{package}/main.dart' as app;

void main() {{
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();
{body}
}}
"""
    }
