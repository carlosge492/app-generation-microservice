"""The generated runtime navigation test.

`conformance.py` proves *something* navigates to each declared target — a static
claim about text in a file. It cannot prove the destination renders, because
`'/capture': (context) => const CaptureScreen()` satisfies it whether or not
`CaptureScreen` survives being built for real.

This generator closes that half: boot the app through its own `main()`, drive its
own `Navigator`, check the destination is on screen. Screen titles are the anchor
because they come from the PRD — the one label that is the same whatever a
generator names its classes, files or routes.
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
        assert f"navigator.pushNamed('/{target}');" in source


def test_the_destination_is_identified_by_its_prd_title():
    """Not by widget class. `<Id>Screen` is TemplateGenerator's convention and
    Claude does not always use it; the title is PRD data."""
    source = _test_source(PRD.model_validate(PRD_BODY))

    assert "find.text('New observation')" in source
    assert "CaptureScreen" not in source


def test_the_app_is_booted_through_its_own_main():
    """Pumping a widget directly would skip the composition root — which is
    exactly where the app-cannot-start bug lived."""
    source = _test_source(PRD.model_validate(PRD_BODY))

    assert "import 'package:field_notes/main.dart' as app;" in source
    assert "await app.main();" in source


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
        assert source.count("void main()") == 1, path
        assert source.count("{") == source.count("}"), path
        assert "testWidgets(" in source, path
