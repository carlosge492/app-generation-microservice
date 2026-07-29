"""The generated app has to be able to start.

Every other check was static or pumped one screen in isolation with its
providers overridden, so nothing ever executed the composition root. It was
broken in every app the generators produced:

    await Firebase.initializeApp();

with no options and no `google-services.json` anywhere in the output. On web
that fails an assertion inside `firebase_core_web` before the first frame; on a
native platform there is no config file to fall back to either. The app
compiled, analysed clean, passed its widget tests, packaged into a signed-ready
APK — and died on launch.

The fix takes the identifiers from `--dart-define` so no buyer's project is
baked into generated source, and passes `null` when none are supplied so a
platform carrying a native config file still works.
"""

from __future__ import annotations

import json

from evals.fixture_generator import FixtureGenerator
from src.ports.templates import TemplateGenerator
from src.prd.schema import PRD

PRD_BODY = json.loads(open("examples/todo_app.prd.json", encoding="utf-8").read())


def _main_dart(generator) -> str:
    prd = PRD.model_validate(PRD_BODY)
    files = generator.wire_logic(prd, "", {}, [])
    return files["lib/main.dart"]


def _code_only(source: str) -> str:
    """The generated Dart with `//` comments removed.

    The templates explain the bare-`initializeApp` trap in a comment that quotes
    the broken call, so a naive substring search finds the documentation and
    reports the bug it is warning about.
    """
    return "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("//")
    )


def test_the_composition_root_does_not_call_initializeapp_bare():
    """The regression. `Firebase.initializeApp()` with nothing passed throws
    wherever there is no native config file, which is everywhere here."""
    code = _code_only(_main_dart(TemplateGenerator()))

    assert "Firebase.initializeApp()" not in code, (
        "a bare initializeApp throws before the first frame"
    )
    assert "Firebase.initializeApp(" in code, "the app still has to initialise Firebase"


def test_firebase_identifiers_come_from_the_environment():
    source = _main_dart(TemplateGenerator())

    for key in (
        "FIREBASE_PROJECT_ID",
        "FIREBASE_API_KEY",
        "FIREBASE_APP_ID",
        "FIREBASE_MESSAGING_SENDER_ID",
    ):
        assert f"String.fromEnvironment('{key}')" in source


def test_an_unconfigured_build_falls_back_to_the_native_config_file():
    """Passing `null` is what keeps a google-services.json build working. Always
    constructing FirebaseOptions from empty strings would break the one case
    that used to work."""
    source = _main_dart(TemplateGenerator())

    assert "isEmpty" in source
    assert "null" in source


def test_the_fixture_generator_starts_too_without_copying_the_template():
    """The fixture generator exists to prove the harness is not grading its own
    conventions, so it must be correct *and* spelled differently. If it drifted
    into being a copy, it would stop being evidence of anything."""
    fixture = _main_dart(FixtureGenerator())
    template = _main_dart(TemplateGenerator())

    assert "Firebase.initializeApp()" not in _code_only(fixture)
    assert "String.fromEnvironment('FIREBASE_PROJECT_ID')" in fixture
    assert fixture != template, "the fixture must not be a copy of the template"
