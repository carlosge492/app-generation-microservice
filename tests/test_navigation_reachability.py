"""A declared `navigate` action has to actually be able to go somewhere.

The template generator listed every screen in the routes table and never pushed
one. `/capture` and `/settings` existed and no widget could reach them: the app
was a form nobody could open. Everything passed — each screen built in
isolation, the routes table was valid Dart, and neither the analyzer nor the
smoke tests have an opinion about whether a route is ever navigated to.

The first version of this check was itself vacuous, and for an instructive
reason: it searched every UI file for `CaptureScreen(`, and found the target
screen's *own* constructor in its *own* file. Every unreachable screen looked
reachable. `test_the_targets_own_file_does_not_prove_reachability` is that bug.
"""

from __future__ import annotations

import json

from src.ports.conformance import check_conformance
from src.prd.schema import PRD

PRD_BODY = json.loads(open("examples/todo_app.prd.json", encoding="utf-8").read())

APP_WITH_ROUTES = """import 'package:flutter/material.dart';
class FieldNotesApp extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      initialRoute: '/observations',
      routes: {
        '/observations': (context) => const ObservationsScreen(),
        '/capture': (context) => const CaptureScreen(),
        '/settings': (context) => const SettingsScreen(),
      },
    );
  }
}
"""

CAPTURE_SCREEN = """import 'package:flutter/material.dart';
class CaptureScreen extends StatelessWidget {
  const CaptureScreen({super.key});
  @override
  Widget build(BuildContext context) => const Scaffold();
}
"""


def _observations(on_pressed: str) -> str:
    return (
        "import 'package:flutter/material.dart';\n"
        "class ObservationsScreen extends StatelessWidget {\n"
        "  const ObservationsScreen({super.key});\n"
        "  @override\n"
        "  Widget build(BuildContext context) => Scaffold(\n"
        "    floatingActionButton: FloatingActionButton(\n"
        f"      onPressed: () {{ {on_pressed} }},\n"
        "      child: const Icon(Icons.add),\n"
        "    ),\n"
        "  );\n"
        "}\n"
    )


def _files(on_pressed: str) -> dict[str, str]:
    return {
        "pubspec.yaml": "name: field_notes\n",
        "lib/ui/app.dart": APP_WITH_ROUTES,
        "lib/ui/observations_screen.dart": _observations(on_pressed),
        "lib/ui/capture_screen.dart": CAPTURE_SCREEN,
    }


def _unreachable(files: dict[str, str]) -> list[str]:
    prd = PRD.model_validate(PRD_BODY)
    return [
        d.message for d in check_conformance(prd, files)
        if d.code == "unreachable_screen"
    ]


def test_a_route_nothing_navigates_to_is_reported():
    """The shipped bug: the FAB prepared a draft and stayed put."""
    found = _unreachable(_files("controller.createDraft();"))

    assert len(found) == 1
    assert "openCapture" in found[0] and "capture" in found[0]


def test_pushing_the_named_route_satisfies_it():
    assert _unreachable(_files("Navigator.pushNamed(context, '/capture');")) == []


def test_constructing_the_target_widget_satisfies_it():
    """`MaterialPageRoute(builder: (_) => const CaptureScreen())` is navigation
    just as much as a named route; the check is deliberately loose about how."""
    assert _unreachable(_files(
        "Navigator.push(context, MaterialPageRoute("
        "builder: (_) => const CaptureScreen()));"
    )) == []


def test_the_targets_own_file_does_not_prove_reachability():
    """The vacuity bug. `capture_screen.dart` contains `class CaptureScreen` and
    `const CaptureScreen({super.key})`, so searching every UI file for the widget
    name found the destination declaring itself and called that a route in."""
    files = _files("controller.createDraft();")
    assert "CaptureScreen(" in files["lib/ui/capture_screen.dart"]

    assert len(_unreachable(files)) == 1, (
        "a screen that only declares itself is still unreachable"
    )


def test_the_routes_table_alone_does_not_prove_reachability():
    """`'/capture': (context) => const CaptureScreen()` declares the
    destination. It is not a way of getting there."""
    files = _files("controller.createDraft();")
    assert "'/capture'" in files["lib/ui/app.dart"]

    assert len(_unreachable(files)) == 1


def test_non_navigate_actions_are_not_required_to_navigate():
    """`saveObservation` is a `create`; demanding a Navigator call for it would
    fail every correct app."""
    files = _files("Navigator.pushNamed(context, '/capture');")

    assert _unreachable(files) == []


def test_the_real_template_output_is_reachable_now():
    """End to end through the actual generator, not a hand-written fixture."""
    from src.ports.templates import TemplateGenerator

    prd = PRD.model_validate(PRD_BODY)
    generator = TemplateGenerator()
    files = dict(generator.build_ui(prd, ""))
    files.update(generator.wire_logic(prd, "", files, []))
    files["pubspec.yaml"] = generator.plan(prd).pubspec

    assert _unreachable(files) == []
