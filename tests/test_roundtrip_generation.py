"""The generated round-trip test must not be written for one generator.

This is the check on the check. Over-fitting to `TemplateGenerator`'s naming is
the most repeated mistake in this repo — at one point 20 of 22 eval failures
were our own conventions, not the model's — and a generated *test* is the
easiest place to do it again, because it looks right until a different generator
runs.

So the two generators here disagree on purpose, and both must work untouched:

| | TemplateGenerator | FixtureGenerator |
| --- | --- | --- |
| deserialiser | `fromSnapshot` | `fromDoc` |
| serialiser | `toMap` | `toJson` |
| file | `lib/providers/…` | `lib/models/…` |
| a date on the wire | `Timestamp` | ISO-8601 `String` |

That last row is why the emitted test round-trips through the app's own pair
rather than reading a hand-written document: both conventions are correct, and
any hand-written document would fail one of them for having different taste.
"""

from __future__ import annotations

import json

from evals.fixture_generator import FixtureGenerator
from src.ports.roundtrip import build_roundtrip_tests
from src.ports.templates import TemplateGenerator
from src.prd.schema import PRD

PRD_BODY = json.loads(open("examples/todo_app.prd.json", encoding="utf-8").read())


def _prd() -> PRD:
    return PRD.model_validate(PRD_BODY)


def _template_files() -> dict[str, str]:
    prd = _prd()
    generator = TemplateGenerator()
    files = dict(generator.wire_logic(prd, "", {}, []))
    files["pubspec.yaml"] = "name: field_notes\n"
    return files


def _fixture_files() -> dict[str, str]:
    prd = _prd()
    generator = FixtureGenerator()
    files = dict(generator.wire_logic(prd, "", {}, []))
    files["pubspec.yaml"] = "name: field_notes\n"
    return files


# --------------------------------------------------------------------------- #
# Both generators, one generator-agnostic test
# --------------------------------------------------------------------------- #


def test_it_finds_the_template_generators_serialiser_pair():
    tests = build_roundtrip_tests(_prd(), _template_files())

    assert tests, "the template generator declares a serialisable model"
    source = next(iter(tests.values()))
    assert ".fromSnapshot(" in source
    assert ".toMap()" in source
    assert "package:field_notes/providers/" in source


def test_it_finds_the_fixture_generators_differently_named_pair():
    """The whole point. Different method names, different directory."""
    tests = build_roundtrip_tests(_prd(), _fixture_files())

    assert tests, "the fixture generator also declares a serialisable model"
    source = next(iter(tests.values()))
    assert ".fromDoc(" in source, "hard-coding fromSnapshot would miss this"
    assert ".toJson()" in source, "hard-coding toMap would miss this"
    assert "package:field_notes/models/" in source


def test_neither_generator_leaks_the_others_names():
    template = next(iter(build_roundtrip_tests(_prd(), _template_files()).values()))
    fixture = next(iter(build_roundtrip_tests(_prd(), _fixture_files()).values()))

    assert "fromDoc" not in template and "toJson" not in template
    assert "fromSnapshot" not in fixture and "toMap" not in fixture


def test_the_write_goes_through_the_apps_own_serialiser():
    """Not a hand-written document. A hand-written one has to pick `Timestamp`
    or ISO string and would fail whichever generator chose the other."""
    source = next(iter(build_roundtrip_tests(_prd(), _template_files()).values()))

    assert ".add(original.toMap())" in source
    # No literal wire values anywhere — those would encode one convention.
    assert "Timestamp.fromDate" not in source
    assert "toIso8601String" not in source


# --------------------------------------------------------------------------- #
# What the emitted Dart says
# --------------------------------------------------------------------------- #


def test_every_prd_field_is_asserted():
    source = next(iter(build_roundtrip_tests(_prd(), _template_files()).values()))

    for field in _prd().models[0].fields:
        assert f"expect(restored.{field.name}, original.{field.name}" in source


def test_the_firestore_assigned_id_is_not_asserted():
    """`id` comes from `doc.id`, which Firestore assigns on write, so the value
    passed in is discarded by design. Asserting it would fail every time."""
    source = next(iter(build_roundtrip_tests(_prd(), _template_files()).values()))

    assert "expect(restored.id" not in source
    assert "id: 'placeholder'" in source, "it still has to be constructible"


def test_the_collection_comes_from_the_prd():
    source = next(iter(build_roundtrip_tests(_prd(), _template_files()).values()))
    assert ".collection('observations')" in source


def test_the_package_name_is_read_from_the_pubspec_not_the_app_name():
    """A generator free to name its package anything would otherwise get an
    import pointing at a package that does not exist."""
    files = _template_files()
    files["pubspec.yaml"] = "name: something_else_entirely\n"

    source = next(iter(build_roundtrip_tests(_prd(), files).values()))

    assert "package:something_else_entirely/" in source
    assert "package:field_notes/" not in source


# --------------------------------------------------------------------------- #
# Refusing to emit rather than emitting something broken
# --------------------------------------------------------------------------- #


def test_a_model_with_no_serialiser_gets_no_test():
    """A missing test is a visible gap. A test that fails because the generator
    named things differently is a false alarm people learn to ignore."""
    files = {"pubspec.yaml": "name: field_notes\n", "lib/main.dart": "void main() {}"}

    assert build_roundtrip_tests(_prd(), files) == {}


def test_a_class_that_can_read_but_not_write_gets_no_test():
    """Half a serialiser pair cannot round trip, and inventing the other half
    would be guessing at the generator's convention."""
    files = {
        "pubspec.yaml": "name: field_notes\n",
        "lib/models/observation.dart": (
            "class Observation {\n"
            "  const Observation({required this.title});\n"
            "  final String title;\n"
            "  factory Observation.fromDoc(DocumentSnapshot<Map<String, dynamic>> d) =>\n"
            "      Observation(title: '');\n"
            "}\n"
        ),
    }

    assert build_roundtrip_tests(_prd(), files) == {}


def test_an_ambiguous_pair_of_classes_is_not_guessed_between():
    """Two serialisable classes and neither named after the model: picking one
    would be a coin flip, and a wrong pick tests the wrong thing silently."""
    body = (
        "class {name} {{\n"
        "  const {name}({{required this.title}});\n"
        "  final String title;\n"
        "  factory {name}.fromDoc(DocumentSnapshot<Map<String, dynamic>> d) =>\n"
        "      {name}(title: '');\n"
        "  Map<String, dynamic> toJson() => <String, dynamic>{{'title': title}};\n"
        "}}\n"
    )
    files = {
        "pubspec.yaml": "name: field_notes\n",
        "lib/models/a.dart": body.format(name="Alpha"),
        "lib/models/b.dart": body.format(name="Beta"),
    }

    assert build_roundtrip_tests(_prd(), files) == {}


# --------------------------------------------------------------------------- #
# Regressions the eval sweep caught and these tests had not
# --------------------------------------------------------------------------- #


def _generated_for(prd_path: str) -> dict[str, str]:
    from src.prd.schema import load_prd

    prd = load_prd(prd_path)
    generator = TemplateGenerator()
    files = dict(generator.wire_logic(prd, "", {}, []))
    files["pubspec.yaml"] = generator.plan(prd).pubspec
    return build_roundtrip_tests(prd, files)


def test_a_model_with_no_date_field_declares_no_clock():
    """`unused_local_variable` is an error, not a warning, so emitting an unused
    `stamp` failed the build for every PRD whose model had no date. Six of the
    eleven eval PRDs, which is how this was found."""
    tests = _generated_for("evals/prds/single_field.prd.json")

    source = next(iter(tests.values()))
    assert "final stamp" not in source, "nothing in this model uses a clock"


def test_a_model_with_a_date_field_still_declares_one():
    tests = _generated_for("evals/prds/all_field_types.prd.json")

    source = next(iter(tests.values()))
    assert "final stamp" in source
    assert "stamp," in source, "and it has to actually be used"


def test_a_model_named_after_a_firestore_type_still_compiles():
    """`tricky_strings` declares a model called `Order`, and cloud_firestore
    exports an `Order`. Unprefixed, that is an `ambiguous_import` error — a
    build failure caused entirely by the buyer's choice of noun. `Query`,
    `Source`, `Settings`, `Filter` and `Transaction` are the same trap."""
    tests = _generated_for("evals/prds/tricky_strings.prd.json")

    source = next(iter(tests.values()))
    assert "import 'package:cloud_firestore/cloud_firestore.dart' as fs;" in source
    assert "fs.FirebaseFirestore.instance" in source
    # The app's own import stays unprefixed so the model name resolves to it.
    assert "as fs;" in source and "/providers/" in source


def test_a_single_oddly_named_class_is_still_used():
    """One serialisable class in the whole app is unambiguous even if it is not
    spelled the way the PRD spells the model."""
    files = {
        "pubspec.yaml": "name: field_notes\n",
        "lib/models/record.dart": (
            "class ObservationRecord {\n"
            "  const ObservationRecord({required this.title});\n"
            "  final String title;\n"
            "  factory ObservationRecord.fromDoc(DocumentSnapshot<Map<String, dynamic>> d) =>\n"
            "      ObservationRecord(title: '');\n"
            "  Map<String, dynamic> toJson() => <String, dynamic>{'title': title};\n"
            "}\n"
        ),
    }

    source = next(iter(build_roundtrip_tests(_prd(), files).values()))

    assert "ObservationRecord.fromDoc(" in source
