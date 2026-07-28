"""Does the grading harness work on code it did not write?

Conformance, smoke-test generation and ownership routing were all developed
against `TemplateGenerator`. They claim to be naming-agnostic — discovering
structure by scanning output rather than predicting it from the PRD — but with
only one generator in existence that claim was untestable, and a harness that
silently only grades its own templates would make the first paid Claude run
return meaningless numbers.

`FixtureGenerator` (evals/fixture_generator.py) emits correct Flutter for the
same PRD while disagreeing with every convention TemplateGenerator uses: page
instead of screen, View instead of Screen, `StreamProvider.autoDispose` instead
of `StreamProvider`, `AsyncNotifierProvider` instead of `StateNotifierProvider`,
and `onGenerateRoute` with route constants instead of a routes map.

Anything reported against the unmodified fixture is a harness bug.
"""

from __future__ import annotations

import pytest

from src.ports.conformance import check_conformance
from src.ports.generator import get_generator
from src.ports.ownership import owner_of
from src.ports.smoke import build_smoke_tests
from src.ports.templates import TemplateGenerator
from src.prd.schema import load_prd

PRD_PATH = "examples/todo_app.prd.json"


@pytest.fixture
def prd():
    return load_prd(PRD_PATH)


@pytest.fixture
def generated(prd):
    """ui, logic — plus the planning output, since conformance checks pubspec."""
    gen = get_generator("fixture")
    ui = gen.build_ui(prd, "", [])
    logic = gen.wire_logic(prd, "", ui, [])
    plan = gen.plan(prd)
    logic = {
        **logic,
        "pubspec.yaml": plan.pubspec,
        "analysis_options.yaml": plan.analysis_options,
    }
    return ui, logic


# --------------------------------------------------------------------------- #
# No false positives on correct, foreign-styled code
# --------------------------------------------------------------------------- #


def test_conformance_is_clean_on_model_style_code(prd, generated):
    ui, logic = generated
    found = check_conformance(prd, {**ui, **logic})
    assert found == [], [f"{d.code}: {d.message}" for d in found]


def test_logic_may_use_the_ordinary_flutter_layout(prd, tmp_path):
    """Logic owns everything under lib/ that is not the widget tree.

    The first live sweep failed 10/10 on lane violations, and most were not
    violations at all: the model wrote lib/models/, lib/services/ and the
    flutterfire-generated lib/firebase_options.dart. Enumerating allowed
    directories had baked in TemplateGenerator's layout, where data classes live
    inside provider files. The invariant CLAUDE.md actually states is that UI and
    state never mix — lib/models/ is state.
    """
    from src.graph.nodes import make_logic_node

    class SpreadOutGenerator(TemplateGenerator):
        def wire_logic(self, prd, design_md, ui_files, diagnostics):
            return {
                "lib/main.dart": "void main() {}",
                "lib/providers/x_providers.dart": "// state",
                "lib/models/x.dart": "// data class",
                "lib/services/firestore_service.dart": "// data access",
                "lib/firebase_options.dart": "// flutterfire configure output",
            }

    node = make_logic_node(SpreadOutGenerator())
    out = node({"prd": prd.model_dump(mode="json"), "design_md": "", "ui_files": {}})

    assert out["phase"] != "failed", out.get("failure")
    assert "lib/models/x.dart" in out["provider_files"]
    assert "lib/firebase_options.dart" in out["provider_files"]


def test_logic_still_may_not_touch_the_widget_tree(prd):
    """Widening the lane must not dissolve the boundary that matters."""
    from src.graph.nodes import make_logic_node

    class TrespassingGenerator(TemplateGenerator):
        def wire_logic(self, prd, design_md, ui_files, diagnostics):
            return {"lib/main.dart": "void main() {}", "lib/ui/app.dart": "// not yours"}

    node = make_logic_node(TrespassingGenerator())
    out = node({"prd": prd.model_dump(mode="json"), "design_md": "", "ui_files": {}})

    # Dropped, never written — but reported, so the loop can repair it.
    assert "lib/ui/app.dart" not in out["provider_files"]
    codes = [d.code for d in out["logic_violations"]]
    assert codes == ["logic_lane_violation"]
    assert owner_of(out["logic_violations"][0]) == "logic", "the culprit must fix it"


def test_genui_still_confined_to_the_widget_tree(prd):
    from src.graph.nodes import make_genui_node

    class OverreachingGenerator(TemplateGenerator):
        def build_ui(self, prd, design_md, diagnostics=None):
            return {"lib/ui/a.dart": "// ok", "lib/models/x.dart": "// not yours"}

    node = make_genui_node(OverreachingGenerator())
    out = node({"prd": prd.model_dump(mode="json"), "design_md": "", "diagnostics": []})

    assert "lib/models/x.dart" not in out["ui_files"]
    assert "lib/ui/a.dart" in out["ui_files"], "in-lane files must survive"
    assert [d.code for d in out["genui_violations"]] == ["genui_lane_violation"]


def test_fixture_uses_a_separate_models_directory(generated):
    """The fixture missed this layout first time round, which is why the live
    sweep found it instead of the free test double."""
    _, logic = generated
    assert "lib/models/observation.dart" in logic


def test_conformance_does_not_depend_on_our_file_names(generated):
    """Sanity-check the fixture really is different, or the test above is empty."""
    ui, logic = generated
    assert "lib/ui/observations_page.dart" in ui
    assert "lib/ui/observations_screen.dart" not in ui
    assert "lib/providers/observations_repository.dart" in logic


# --------------------------------------------------------------------------- #
# Smoke-test discovery
# --------------------------------------------------------------------------- #


def test_smoke_tests_discover_foreign_widget_names(prd, generated):
    ui, logic = generated
    tests = build_smoke_tests(prd, {**ui, **logic})

    assert set(tests) == {
        "test/observations_view_smoke_test.dart",
        "test/capture_view_smoke_test.dart",
        "test/settings_view_smoke_test.dart",
    }


def test_autodispose_providers_are_still_overridden(prd, generated):
    """Regression: `StreamProvider.autoDispose<...>` was not matched, so no
    override was emitted. The screen then reached real Firestore, the generated
    `.when(error:)` handler swallowed the failure, and the test passed while
    asserting nothing — coverage that could not fail."""
    ui, logic = generated
    body = build_smoke_tests(prd, {**ui, **logic})["test/observations_view_smoke_test.dart"]

    assert "observationsStreamProvider.overrideWith" in body
    # The element type must be the model, not the provider kind.
    assert "Stream<List<Observation>>.value(<Observation>[])" in body
    assert "container.read(observationsStreamProvider.future), completes" in body


def test_screens_without_async_providers_get_no_spurious_imports(prd, generated):
    ui, logic = generated
    body = build_smoke_tests(prd, {**ui, **logic})["test/settings_view_smoke_test.dart"]
    assert "providers/" not in body


# --------------------------------------------------------------------------- #
# Faults injected into model-style code are still caught, and correctly owned
# --------------------------------------------------------------------------- #


def _mutate(ui, logic, *, drop_ui=None, drop_logic=None, replace=None):
    ui, logic = dict(ui), dict(logic)
    if drop_ui:
        ui = {k: v for k, v in ui.items() if drop_ui not in k}
    if drop_logic:
        logic = {k: v for k, v in logic.items() if drop_logic not in k}
    if replace:
        path, old, new = replace
        target = ui if path in ui else logic
        target[path] = target[path].replace(old, new)
    return {**ui, **logic}


@pytest.mark.parametrize(
    "label, kwargs, code, expected_owner",
    [
        ("dropped screen", {"drop_ui": "settings"}, "missing_screen", "genui"),
        ("dropped provider", {"drop_logic": "repository"}, "missing_provider", "logic"),
        (
            "renamed collection",
            {"replace": ("lib/providers/observations_repository.dart",
                         "'observations'", "'obs_v2'")},
            "wrong_collection", "logic",
        ),
        (
            "dropped form field",
            {"replace": ("lib/ui/capture_page.dart", "'verified'", "'ignored'")},
            "missing_form_field", "genui",
        ),
        (
            "unwired helper",
            {"replace": ("lib/ui/settings_page.dart", "class SettingsView",
                         "String formatBadge(int n) { return ''; }\n\nclass SettingsView")},
            "unwired_declaration", "genui",
        ),
    ],
)
def test_injected_faults_are_caught_with_the_right_owner(
    prd, generated, label, kwargs, code, expected_owner
):
    ui, logic = generated
    found = check_conformance(prd, _mutate(ui, logic, **kwargs))

    matching = [d for d in found if d.code == code]
    assert matching, f"{label}: expected {code}, got {[d.code for d in found]}"
    assert owner_of(matching[0]) == expected_owner
