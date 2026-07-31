"""The generated runtime navigation test.

`conformance.py` proves *something* navigates to each declared target — a static
claim about text in a file. It cannot prove the destination renders, because
`'/capture': (context) => const CaptureScreen()` satisfies it whether or not
`CaptureScreen` survives being built for real.

This generator closes that half twice over: boot the app through its own
`main()`, drive its own `Navigator` to the target and check the destination is on
screen, then go to the source screen and check some affordance on it actually
gets a user there. Screen titles are the anchor because they come from the PRD —
the one label that is the same whatever a generator names its classes, files or
routes.

These tests are about the shape of the emitted Dart. That the emitted Dart tests
anything is a separate claim, and a claim only a browser can settle:
`scripts/verify_navigation_flow.py` runs it against a real app and against a
mutant with its affordance removed.
"""

from __future__ import annotations

import json

from src.ports.navigation import build_navigation_test
from src.prd.schema import PRD, load_prd

PRD_BODY = json.loads(open("examples/todo_app.prd.json", encoding="utf-8").read())
FILES = {"pubspec.yaml": "name: field_notes\n"}


def _test_source(prd: PRD, files: dict[str, str] | None = None) -> str:
    generated = build_navigation_test(prd, files or FILES)
    return generated["integration_test/navigation_test.dart"]


def test_one_case_per_declared_navigate_action():
    prd = PRD.model_validate(PRD_BODY)
    source = _test_source(prd)

    expected = [
        (s.id, a.target) for s in prd.screens for a in s.actions
        if a.kind == "navigate" and a.target
    ]
    assert expected, "the example PRD declares navigation"
    for source_id, target in expected:
        assert f"{source_id} -> {target}" in source
        assert f"await _open(tester, '/{target}', destination);" in source


def test_each_action_gets_both_a_push_case_and_a_tap_case():
    """They are different claims. Pushing the route proves the destination
    renders; it says nothing about whether a user could ever get there. The
    static `unreachable_screen` check says something navigates there, but not
    that the thing doing it is reachable on screen."""
    prd = PRD.model_validate(PRD_BODY)
    source = _test_source(prd)

    for screen in prd.screens:
        for action in screen.actions:
            if action.kind != "navigate" or not action.target:
                continue
            assert f"{screen.id} -> {action.target} renders" in source
            assert f"a tap on {screen.id} reaches {action.target}" in source
            assert f"_tapReaches(\n      tester,\n      '/{screen.id}'," in source


def test_tappability_is_not_a_list_of_button_classes():
    """`InkResponse` is underneath TextButton, ElevatedButton, IconButton,
    FloatingActionButton and ListTile, and CupertinoButton/GestureDetector cover
    the rest. Naming the buttons would encode TemplateGenerator's taste — and
    would miss the FAB, whose only child is an icon, which is the affordance
    every generated list screen actually navigates from."""
    source = _test_source(PRD.model_validate(PRD_BODY))

    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("///")
    )
    assert "widget is material.InkResponse" in code
    assert "widget is cupertino.CupertinoButton" in code
    assert "widget is widgets.GestureDetector" in code
    for named in ("TextButton", "ElevatedButton", "FloatingActionButton"):
        assert named not in code, f"{named} is one generator's choice"


def test_arrival_is_the_route_name_first_and_the_title_only_as_a_fallback():
    """A button labelled with the destination's title is good UI. If arrival
    meant 'the title is on screen' the test would pass before tapping anything,
    so the title only counts when it was not already visible."""
    source = _test_source(PRD.model_validate(PRD_BODY))

    assert "_currentRoute(tester) == target" in source
    assert "final titleWasVisible = title.evaluate().isNotEmpty;" in source
    assert "if (!titleWasVisible && title.evaluate().isNotEmpty)" in source


def test_every_tap_attempt_restarts_from_the_source_screen():
    """Otherwise it is one stateful sequence: a tap that opens a dialog or
    mutates state changes what the next tap hits, and a failure stops meaning
    anything. Re-opening makes each tap an independent experiment."""
    source = _test_source(PRD.model_validate(PRD_BODY))

    loop = source.split("for (var i = 0; i < budget; i++) {", 1)[1]
    assert loop.index("await _open(tester, source, sourceFinder);") < loop.index(
        "tester.tap("
    )


def test_settling_is_bounded():
    """`pumpAndSettle` defaults to a ten-minute timeout and a screen waiting on
    data never settles, so an unbounded settle inside a per-candidate loop would
    hang the run rather than fail it."""
    source = _test_source(PRD.model_validate(PRD_BODY))

    assert "const Duration(seconds: 5)," in source
    assert "await tester.pumpAndSettle();" not in source


def test_the_home_screen_is_not_assumed_to_live_at_its_own_route():
    """The bug Opus 5 exposed, and the only one this whole exercise was for.

    `'/'` is the conventional Flutter name for the home screen. Opus filed
    `inbox` there and gave `onGenerateRoute` a default branch rendering a
    "Route not found" placeholder — so pushing `'/inbox'` produced a page that
    was neither an error nor the screen, and the test tapped around it and
    reported a perfectly navigable app unreachable. `TemplateGenerator`'s
    `initialRoute: '/observations'` is the unusual convention; the check had
    over-fitted to it.

    Both halves therefore pop to the first route before pushing anything."""
    source = _test_source(PRD.model_validate(PRD_BODY))

    assert "popUntil((existing) => existing.isFirst)" in source
    # The push must come *after* the check that the screen is already showing,
    # or the home case pushes an unknown route before ever looking.
    body = source.split("Future<bool> _open(", 1)[1]
    assert body.index("if (title.evaluate().isNotEmpty)") < body.index("pushNamed(route)")


def test_a_tap_case_knows_which_screen_it_starts_from():
    """Without the source screen's own title there is no way to tell "the app
    put home somewhere else" from "the target really is unreachable" — the two
    produced identical output before, and one of them is a false accusation."""
    prd = PRD.model_validate(PRD_BODY)
    source = _test_source(prd)

    for screen in prd.screens:
        for action in screen.actions:
            if action.kind == "navigate" and action.target:
                assert f"'/{screen.id}',\n      '{screen.title}'," in source


def test_the_tap_budget_reaches_the_generated_dart_as_a_number():
    """The helpers are one interpolated block, so a formatting slip would ship
    `_tapBudget` as an undefined identifier — which only `flutter analyze`
    would catch, and only on a PRD that declares navigation."""
    source = _test_source(PRD.model_validate(PRD_BODY))

    assert "const int _tapBudget = 40;" in source
    assert "%(budget)" not in source


def test_the_destination_is_identified_by_its_prd_title():
    """Not by widget class. `<Id>Screen` is TemplateGenerator's convention and
    Claude does not always use it; the title is PRD data."""
    source = _test_source(PRD.model_validate(PRD_BODY))

    assert "find.text('New observation')" in source
    assert "CaptureScreen" not in source


def test_the_entry_point_is_not_awaited():
    """`void main()` and `Future<void> main()` are both idiomatic Flutter, and
    `await` on the first is a compile error — `use_of_void_result` — which fails
    the entire build rather than just this test.

    Found when Sonnet wrote `void main() async` where it had previously written
    `Future<void> main() async`: the generated app was fine and the harness's
    own test would not compile against it. Third bug of this shape, after the
    missing integration_test dependency and the home-route assumption."""
    source = _test_source(PRD.model_validate(PRD_BODY))

    assert "app.main();" in source
    assert "await app.main()" not in source


def test_the_app_is_booted_through_its_own_main():
    """Pumping a widget directly would skip the composition root — which is
    exactly where the app-cannot-start bug lived."""
    source = _test_source(PRD.model_validate(PRD_BODY))

    assert "import 'package:field_notes/main.dart' as app;" in source
    assert "app.main();" in source


def test_the_package_name_comes_from_the_pubspec():
    source = _test_source(
        PRD.model_validate(PRD_BODY), {"pubspec.yaml": "name: something_else\n"}
    )

    assert "package:something_else/main.dart" in source


def test_a_prd_with_no_navigation_gets_no_test():
    """`minimal` declares no navigate action. A test with no cases would be a
    file that always passes, which is worse than no file."""
    assert build_navigation_test(load_prd("evals/prds/minimal.prd.json"), FILES) == {}


def test_the_driver_is_emitted_alongside():
    """A navigation test with no `flutter drive` entry point cannot be run."""
    generated = build_navigation_test(PRD.model_validate(PRD_BODY), FILES)

    assert "test_driver/integration_test.dart" in generated
    assert "integrationDriver()" in generated["test_driver/integration_test.dart"]


def test_a_title_containing_a_quote_is_escaped():
    """Titles are free text. An unescaped apostrophe closes the Dart string and
    the generated test stops compiling — which the analyzer would report against
    generated code rather than as the escaping bug it is."""
    body = json.loads(json.dumps(PRD_BODY))
    for screen in body["screens"]:
        if screen["id"] == "capture":
            screen["title"] = "Bob's $notes\\path"

    source = _test_source(PRD.model_validate(body))

    assert r"\'" in source
    assert r"\$" in source
    assert r"\\" in source


def test_every_eval_prd_generates_something_parseable():
    """Balanced braces and one main(), across all the PRDs chosen to break things."""
    import glob

    for path in sorted(glob.glob("evals/prds/*.prd.json")):
        try:
            prd = load_prd(path)
        except Exception:
            continue  # invalid_unknown_model is rejected on purpose
        generated = build_navigation_test(prd, FILES)
        if not generated:
            continue
        source = generated["integration_test/navigation_test.dart"]
        # Comments stripped first. A comment mentioning `void main()` — this
        # file's own explanation of why the entry point is not awaited does —
        # would otherwise be counted as a second entry point. That is the
        # fourth time today a pattern has matched prose instead of code.
        code = "\n".join(
            line for line in source.splitlines()
            if not line.strip().startswith("//") and not line.strip().startswith("///")
        )
        assert code.count("void main()") == 1, path
        assert code.count("{") == code.count("}"), path
        assert "testWidgets(" in code, path
