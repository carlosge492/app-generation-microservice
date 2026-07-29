"""Firestore's wire types are not the app's Dart types.

The bug this locks down shipped in every generated app with a `date` field and
was invisible to everything the pipeline checked. `flutter analyze` was clean —
`data['x'] as DateTime?` is perfectly well-typed Dart. The generated widget
smoke tests passed, because they never put a real document through the mapper.
It took a live Firestore to see it:

    TypeError: Instance of 'Timestamp':
    type 'Timestamp' is not a subtype of type 'DateTime?'

Firestore has no date type. A `DateTime` goes in as a `Timestamp` and comes back
as a `Timestamp`, so the first time a generated app read back a document it had
written itself, the list screen threw. `scripts/verify_firestore_roundtrip.py`
proves the fix against the emulator; these tests are the cheap guard that keeps
it fixed.
"""

from __future__ import annotations

import json

from src.ports.templates import TemplateGenerator, wire_read, wire_write
from src.prd.schema import PRD

PRD_BODY = json.loads(open("examples/todo_app.prd.json", encoding="utf-8").read())


def _generated_providers() -> dict[str, str]:
    """The provider files the Logic agent would write for the example PRD."""
    prd = PRD.model_validate(PRD_BODY)
    return TemplateGenerator().wire_logic(prd, "", {}, [])


# --------------------------------------------------------------------------- #
# The expressions themselves
# --------------------------------------------------------------------------- #


def test_a_date_is_read_back_as_a_timestamp_and_converted():
    """The whole bug in one line. `as DateTime?` type-checks and throws."""
    expression = wire_read("recordedAt", "date")

    assert "as Timestamp?" in expression
    assert ".toDate()" in expression
    assert "as DateTime?" not in expression


def test_a_date_is_written_as_a_timestamp():
    assert wire_write("recordedAt", "date") == "Timestamp.fromDate(recordedAt)"


def test_the_other_types_are_unchanged_because_their_wire_type_matches():
    """Only dates differ. Wrapping the rest would be noise that reads as fear."""
    assert wire_read("title", "text") == "(data['title'] as String?) ?? ''"
    assert wire_read("count", "number") == "(data['count'] as num?) ?? 0"
    assert wire_read("verified", "bool") == "(data['verified'] as bool?) ?? false"
    for name, kind in (("title", "text"), ("count", "number"), ("verified", "bool")):
        assert wire_write(name, kind) == name


def test_read_and_write_are_inverses_for_every_supported_type():
    """The property that actually matters: a field must come back the way it
    went in. The fixture generator stores dates as ISO strings and parses them
    back, which is equally valid — what is not valid is writing one shape and
    reading another."""
    for kind in ("text", "number", "bool", "date"):
        written = wire_write("f", kind)
        read = wire_read("f", kind)
        assert ("Timestamp" in written) == ("Timestamp" in read), (
            f"{kind} is written and read through different types"
        )


# --------------------------------------------------------------------------- #
# What actually reaches the generated file
# --------------------------------------------------------------------------- #


def test_the_generated_provider_never_casts_a_firestore_date_to_datetime():
    """The regression, checked where it bit: the emitted Dart."""
    files = _generated_providers()
    providers = [
        source for path, source in files.items() if path.startswith("lib/providers/")
    ]

    assert providers, "the PRD declares a model, so a provider should exist"
    for source in providers:
        assert "as DateTime?" not in source, (
            "a Firestore date cast straight to DateTime throws at runtime"
        )


def test_the_generated_provider_converts_the_timestamp_it_reads():
    files = _generated_providers()
    provider = next(
        source for path, source in files.items() if path.startswith("lib/providers/")
    )

    # `recordedAt` is the PRD's only `date` field.
    assert "(data['recordedAt'] as Timestamp?)?.toDate()" in provider
    assert "'recordedAt': Timestamp.fromDate(recordedAt)" in provider
