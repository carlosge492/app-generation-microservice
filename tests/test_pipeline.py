from __future__ import annotations

import glob
import json
import re

import pytest
import yaml

from src.build.pipeline import BuildResult, run_build
from src.build.scaffold import _split_application_id
from src.graph.builder import build_graph
from src.graph.state import initial_state
from src.payments.x402 import PaymentNotVerified, is_verified
from src.ports.analyzer import Diagnostic, FlutterAnalyzer, StubAnalyzer, is_fatal
from src.ports.conformance import _top_level_names, check_conformance
from src.ports.generator import Plan
from src.ports.ownership import owner_of
from src.ports.runtime import _parse_runner_output
from src.ports.smoke import build_smoke_tests
from src.ports.templates import (
    TemplateGenerator,
    dart_string,
    repair_pubspec_description,
    repair_stray_prose_line,
)
from src.prd.schema import PRD, load_prd

PRD_PATH = "examples/todo_app.prd.json"


@pytest.fixture
def prd() -> PRD:
    return load_prd(PRD_PATH)


def run(prd: PRD, build_dir, *, fault=None, max_repairs=3):
    app = build_graph(
        TemplateGenerator(fault=fault),
        StubAnalyzer(),
        max_repairs=max_repairs,
        dry_run=True,
    )
    return app.invoke(initial_state(prd.model_dump(mode="json"), str(build_dir)))


# --------------------------------------------------------------------------- #
# PRD contract
# --------------------------------------------------------------------------- #


def test_example_prd_validates(prd):
    assert prd.app_name == "Field Notes"
    assert [s.id for s in prd.screens] == ["observations", "capture", "settings"]


def test_rejects_unknown_model_reference(prd):
    raw = json.loads(open(PRD_PATH, encoding="utf-8").read())
    raw["screens"][0]["model"] = "Ghost"
    with pytest.raises(ValueError, match="unknown model"):
        PRD.model_validate(raw)


def test_rejects_duplicate_screen_ids(prd):
    raw = json.loads(open(PRD_PATH, encoding="utf-8").read())
    raw["screens"].append(raw["screens"][0])
    with pytest.raises(ValueError, match="unique"):
        PRD.model_validate(raw)


def test_rejects_navigate_to_missing_screen():
    with pytest.raises(ValueError, match="unknown screens"):
        PRD.model_validate({
            "app_name": "X",
            "package_name": "com.example.x",
            "screens": [{
                "id": "home", "title": "Home", "kind": "list",
                "actions": [{"name": "go", "kind": "navigate", "target": "nowhere"}],
            }],
        })


def test_rejects_non_reverse_dns_package_name():
    with pytest.raises(ValueError, match="reverse-DNS"):
        PRD.model_validate({
            "app_name": "X", "package_name": "notdns",
            "screens": [{"id": "home", "title": "Home", "kind": "list"}],
        })


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_clean_build_reaches_packaging(prd, tmp_path):
    final = run(prd, tmp_path)

    assert final["phase"] == "done"
    assert final["diagnostics"] == []
    assert final["repair_attempts"] == 0

    for rel in [
        "pubspec.yaml",
        "DESIGN.md",
        "lib/main.dart",
        "lib/ui/app.dart",
        "lib/ui/observations_screen.dart",
        "lib/ui/capture_screen.dart",
        "lib/ui/settings_screen.dart",
        "lib/providers/observation_providers.dart",
    ]:
        assert (tmp_path / rel).exists(), f"missing {rel}"


def test_ownership_lanes_are_respected(prd, tmp_path):
    final = run(prd, tmp_path)
    assert all(p.startswith("lib/ui/") for p in final["ui_files"])
    assert all(
        p.startswith("lib/providers/") or p == "lib/main.dart"
        for p in final["provider_files"]
    )


def test_ui_never_imports_firebase(prd, tmp_path):
    run(prd, tmp_path)
    for path in (tmp_path / "lib" / "ui").rglob("*.dart"):
        assert "firebase" not in path.read_text(encoding="utf-8").lower()


# --------------------------------------------------------------------------- #
# Repair loop
# --------------------------------------------------------------------------- #


def test_provider_desync_is_repaired(prd, tmp_path):
    """The UI references a provider with no basis in the PRD.

    Logic cannot honestly satisfy that, so it changes nothing; the round stalls
    and the router escalates to GenUI, which drops the reference. Two rounds,
    and the app is actually correct at the end of them.
    """
    final = run(prd, tmp_path, fault="undefined_provider")

    assert final["phase"] == "done", final.get("failure")
    assert final["diagnostics"] == []
    assert final["repair_attempts"] == 2, "expected a stalled Logic pass, then GenUI"
    assert any("stalled" in line for line in final["log"])

    # The fix is real: nothing references the bogus provider any more...
    for path in (tmp_path / "lib").rglob("*.dart"):
        assert "unsyncedDraftProvider" not in path.read_text(encoding="utf-8"), path
    # ...and no provider was fabricated to paper over it.
    assert not (tmp_path / "lib/providers/repair_providers.dart").exists()


def test_repair_never_fabricates_a_provider(prd, tmp_path):
    """Regression: the loop used to answer `undefined_provider` by declaring a
    throwaway StateProvider named after whatever the UI referenced. That went
    green without making the app any more correct."""
    final = run(prd, tmp_path, fault="undefined_provider")

    declared = "\n".join(final["provider_files"].values())
    assert "StateProvider<Map<String, dynamic>>" not in declared
    assert all("repair" not in p for p in final["provider_files"])


def test_stalled_round_escalates_to_the_other_agent(prd, tmp_path):
    """A diagnostic its owner cannot fix must not burn the whole budget."""
    from src.graph.nodes import make_router

    stuck = [Diagnostic("error", "lib/providers/x.dart", 1, "ui_in_logic", "x")]
    state = {"diagnostics": stuck, "repair_attempts": 1}

    router = make_router(max_repairs=3)
    assert router(state) == "repair_logic"
    assert router({**state, "stalled": True}) == "repair_ui"


def test_repair_budget_is_enforced(prd, tmp_path):
    final = run(prd, tmp_path, fault="undefined_provider", max_repairs=0)

    assert final["diagnostics"], "expected the build to stay red"
    # The counter ticks once per failed analysis, so a budget of 0 means the
    # first failure is terminal.
    assert final["repair_attempts"] == 1


def test_ui_owned_fault_routes_to_genui(prd, tmp_path):
    """A setState violation lives in lib/ui/, which only GenUI may write."""
    final = run(prd, tmp_path, fault="setstate")

    assert final["phase"] == "done", final.get("failure")
    assert final["diagnostics"] == []
    assert any("genui: repair pass" in line for line in final["log"])


def test_unfixable_pubspec_fails_without_burning_the_budget(prd, tmp_path):
    """Planning output is frozen; no downstream agent can repair it."""

    class BadPlanGenerator(TemplateGenerator):
        def plan(self, prd):
            base = super().plan(prd)
            # Structurally broken, not merely an unquoted description — that
            # case is now repaired automatically, so it no longer exercises
            # the frozen-planning-output path.
            return Plan(
                design_md=base.design_md,
                pubspec="name: x\ndeps: [unclosed\n  nested: {\n",
                analysis_options=base.analysis_options,
            )

    app = build_graph(BadPlanGenerator(), StubAnalyzer(), max_repairs=3, dry_run=True)
    final = app.invoke(initial_state(prd.model_dump(mode="json"), str(tmp_path)))

    assert final["diagnostics"]
    assert "unparseable_pubspec" in {d.code for d in final["diagnostics"]}
    assert final["repair_attempts"] == 1, "should fail fast, not loop"


# --------------------------------------------------------------------------- #
# Analyzer rules
# --------------------------------------------------------------------------- #


def test_analyzer_flags_setstate_outside_animation(tmp_path):
    (tmp_path / "lib" / "ui").mkdir(parents=True)
    (tmp_path / "pubspec.yaml").write_text("name: x\nenvironment:\ndependencies:\n")
    (tmp_path / "lib/ui/bad.dart").write_text("void f() { setState(() {}); }")

    codes = {d.code for d in StubAnalyzer().analyze(tmp_path)}
    assert "setstate_in_ui" in codes


def test_analyzer_allows_marked_animation_toggle(tmp_path):
    (tmp_path / "lib" / "ui").mkdir(parents=True)
    (tmp_path / "pubspec.yaml").write_text("name: x\nenvironment:\ndependencies:\n")
    (tmp_path / "lib/ui/ok.dart").write_text(
        "void f() { setState(() {}); } // localized animation toggle"
    )

    codes = {d.code for d in StubAnalyzer().analyze(tmp_path)}
    assert "setstate_in_ui" not in codes


def test_analyzer_flags_widget_build_in_providers(tmp_path):
    (tmp_path / "lib" / "providers").mkdir(parents=True)
    (tmp_path / "pubspec.yaml").write_text("name: x\nenvironment:\ndependencies:\n")
    (tmp_path / "lib/providers/bad.dart").write_text("class C { Widget build(x) { return x; } }")

    codes = {d.code for d in StubAnalyzer().analyze(tmp_path)}
    assert "ui_in_logic" in codes


# --------------------------------------------------------------------------- #
# Unwired-code escalation
# --------------------------------------------------------------------------- #


def _warning(code: str, file: str = "lib/ui/home_screen.dart") -> Diagnostic:
    return Diagnostic("warning", file, 3, code, "not referenced")


@pytest.mark.parametrize(
    "code", ["unused_element", "unused_field", "unused_local_variable", "dead_code"]
)
def test_orphaned_code_warnings_break_the_build(code):
    """In generated code an unreferenced declaration is an unwired hallucination,
    not untidiness: the model wrote a helper and never called it."""
    assert is_fatal(_warning(code)) is True


@pytest.mark.parametrize("code", ["prefer_const_constructors", "avoid_print", "todo"])
def test_ordinary_style_warnings_do_not_break_the_build(code):
    assert is_fatal(_warning(code)) is False


def test_errors_remain_fatal_regardless_of_code():
    assert is_fatal(Diagnostic("error", "lib/ui/a.dart", 1, "anything", "boom")) is True


def test_escalated_warning_routes_to_its_owner():
    """Escalation must not bypass ownership routing."""
    assert owner_of(_warning("unused_element", "lib/ui/home_screen.dart")) == "genui"
    assert owner_of(_warning("unused_element", "lib/providers/x.dart")) == "logic"


def test_qa_node_escalates_a_warning_into_a_failing_build(prd, tmp_path):
    """End to end: a warning-severity diagnostic alone must stop the build."""

    class DeadCodeAnalyzer:
        def analyze(self, project_dir):
            return [_warning("unused_element")]

    app = build_graph(TemplateGenerator(), DeadCodeAnalyzer(), max_repairs=0, dry_run=True)
    final = app.invoke(initial_state(prd.model_dump(mode="json"), str(tmp_path)))

    assert final["diagnostics"], "a lone warning should have failed the build"
    assert final["diagnostics"][0].code == "unused_element"
    assert any("escalated as unwired/dead code" in line for line in final["log"])


@pytest.mark.parametrize(
    "path, snippet, name",
    [
        ("lib/ui/settings_screen.dart",
         "\nString formatSubtitle(String raw) { return raw.trim(); }\n", "formatSubtitle"),
        ("lib/ui/app.dart",
         "\nclass OrphanedCard extends StatelessWidget {}\n", "OrphanedCard"),
        ("lib/providers/observation_providers.dart",
         "\nfinal ghostProvider = StateProvider<int>((ref) => 0);\n", "ghostProvider"),
    ],
)
def test_public_unwired_declarations_are_caught(path, snippet, name):
    """`dart analyze` reports unused_element for PRIVATE declarations only — it
    cannot know an external package won't import a public one. A generated app
    has no external importers, so that blind spot is ours to cover."""
    prd = load_prd(PRD_PATH)
    gen = TemplateGenerator()
    ui = gen.build_ui(prd, "", [])
    logic = gen.wire_logic(prd, "", ui, [])
    files = {**ui, **logic}
    files[path] = files[path] + snippet

    found = [d for d in check_conformance(prd, files) if d.code == "unwired_declaration"]
    assert [d for d in found if name in d.message], f"{name} was not flagged"


def test_unwired_check_has_no_false_positives_on_generated_output():
    """The check is worthless if it cries wolf on our own templates."""
    for name in sorted(glob.glob("evals/prds/*.json")) + [PRD_PATH]:
        if "invalid_" in name:
            continue
        prd = load_prd(name)
        gen = TemplateGenerator()
        ui = gen.build_ui(prd, "", [])
        logic = gen.wire_logic(prd, "", ui, [])
        found = [
            d for d in check_conformance(prd, {**ui, **logic})
            if d.code == "unwired_declaration"
        ]
        assert found == [], f"{name}: {[d.message for d in found]}"


@pytest.mark.parametrize("name", ["toMap", "toJson", "fromJson", "copyWith", "fromSnapshot"])
def test_serialization_boilerplate_is_never_flagged(name):
    """Data-class contracts, not dead code: `toMap` exists so the model *can* be
    persisted, whether or not this PRD has a screen that writes one. Reference
    count measures the PRD's feature set, not whether the data layer is sound."""
    prd = load_prd(PRD_PATH)
    gen = TemplateGenerator()
    ui = gen.build_ui(prd, "", [])
    logic = gen.wire_logic(prd, "", ui, [])
    # Declared at top level, referenced by nothing — the shape that would
    # otherwise trip the check.
    logic["lib/providers/observation_providers.dart"] += (
        f"\nMap<String, dynamic> {name}(Object o) {{ return <String, dynamic>{{}}; }}\n"
    )

    found = [d for d in check_conformance(prd, {**ui, **logic})
             if d.code == "unwired_declaration"]
    assert found == [], f"{name} should be exempt, got {[d.message for d in found]}"


def test_class_methods_are_not_treated_as_top_level():
    """Regression: a `\\s` inside a character class consumed indentation and made
    every method look like a top-level declaration."""
    body = "class A {\n  Map<String, dynamic> toMap() { return {}; }\n  void update() {}\n}\n"
    assert _top_level_names(body) == {"A"}


def test_lint_config_also_escalates_at_the_analyzer_level():
    """Belt and braces: the policy holds even if the QA node is bypassed."""
    options = TemplateGenerator().plan(load_prd(PRD_PATH)).analysis_options
    for code in ("unused_element", "unused_field", "dead_code"):
        assert f"{code}: error" in options


# --------------------------------------------------------------------------- #
# Widget smoke tests
# --------------------------------------------------------------------------- #


def _generated(prd_path=PRD_PATH):
    prd = load_prd(prd_path)
    gen = TemplateGenerator()
    ui = gen.build_ui(prd, "", [])
    logic = gen.wire_logic(prd, "", ui, [])
    return prd, {**ui, **logic}


def test_smoke_test_generated_per_screen_but_not_for_app():
    prd, files = _generated()
    tests = build_smoke_tests(prd, files)

    assert set(tests) == {
        "test/observations_screen_smoke_test.dart",
        "test/capture_screen_smoke_test.dart",
        "test/settings_screen_smoke_test.dart",
    }


def test_smoke_test_only_imports_what_the_screen_uses():
    """`unused_import` is escalated to an error and `flutter analyze` reads
    test/, so a sloppy test file would fail the build it is meant to validate."""
    prd, files = _generated()
    tests = build_smoke_tests(prd, files)

    # The settings screen touches no providers, so it must not import any.
    settings = tests["test/settings_screen_smoke_test.dart"]
    assert "providers/" not in settings
    # The list screen does.
    assert "providers/observation_providers.dart" in tests[
        "test/observations_screen_smoke_test.dart"
    ]


def test_smoke_test_asserts_the_data_layer_resolves():
    """takeException() alone is too weak: the screens catch provider errors with
    `.when(error:)`, so a wholly broken data layer would still pass."""
    prd, files = _generated()
    body = build_smoke_tests(prd, files)["test/observations_screen_smoke_test.dart"]

    assert "observationListProvider.overrideWith" in body
    assert "container.read(observationListProvider.future), completes" in body


def test_smoke_test_uses_the_matching_app_wrapper():
    """A Cupertino screen pumped inside a MaterialApp (or vice versa) would
    throw on MaterialLocalizations, which is the bug class this catches."""
    _, material = _generated()
    prd_m = load_prd(PRD_PATH)
    assert "MaterialApp(home:" in build_smoke_tests(prd_m, material)[
        "test/observations_screen_smoke_test.dart"
    ]

    prd_c, cupertino = _generated("evals/prds/cupertino.prd.json")
    body = build_smoke_tests(prd_c, cupertino)["test/articles_screen_smoke_test.dart"]
    assert "CupertinoApp(home:" in body
    assert "MaterialApp" not in body


def test_smoke_tests_are_discovered_not_predicted():
    """Names come from scanning the output, so a differently-named generator
    still gets valid tests. This is what keeps them usable for LLM output."""
    prd = load_prd(PRD_PATH)
    files = {
        "lib/ui/weird_name.dart": "class TotallyDifferentScreen extends ConsumerWidget {}\n",
        "lib/providers/p.dart":
            "final oddProvider = StreamProvider<List<Thing>>((ref) => x);\n",
    }
    files["lib/ui/weird_name.dart"] += "  ref.watch(oddProvider);\n"

    tests = build_smoke_tests(prd, files)
    body = next(iter(tests.values()))
    assert "TotallyDifferentScreen" in body
    assert "oddProvider.overrideWith" in body
    assert "Stream<List<Thing>>" in body


# --------------------------------------------------------------------------- #
# Test runner
# --------------------------------------------------------------------------- #


def test_runner_parses_failing_tests(tmp_path):
    (tmp_path / "test").mkdir()
    output = (
        "00:01 +2 -1: Some tests failed.\n"
        "\n"
        "Failing tests:\n"
        f"  {tmp_path.as_posix()}/test/home_screen_smoke_test.dart: HomeScreen builds\n"
    )
    found = _parse_runner_output(output, tmp_path)

    assert len(found) == 1
    assert found[0].code == "smoke_failure"
    assert found[0].file == "test/home_screen_smoke_test.dart"
    assert "HomeScreen builds" in found[0].message


def test_runner_never_reports_green_on_unparseable_failure(tmp_path):
    (tmp_path / "test").mkdir()
    found = _parse_runner_output("everything exploded, no summary", tmp_path)
    assert len(found) == 1 and found[0].code == "smoke_failure"


def test_smoke_failure_routes_to_genui():
    d = Diagnostic("error", "test/home_screen_smoke_test.dart", 0, "smoke_failure", "x")
    assert owner_of(d) == "genui"


def test_qa_node_ships_the_smoke_tests(prd, tmp_path):
    final = run(prd, tmp_path)
    assert set(final["test_files"]), "smoke tests should be part of the deliverable"
    for rel in final["test_files"]:
        assert (tmp_path / rel).exists(), f"{rel} was not written to disk"


# --------------------------------------------------------------------------- #
# x402 payment gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [False, None, 0, "", "false", "true", "1", 1])
def test_only_literal_true_opens_the_gate(value):
    assert is_verified(value) is False


def test_literal_true_opens_the_gate():
    assert is_verified(True) is True


def test_build_refuses_without_payment(tmp_path):
    with pytest.raises(PaymentNotVerified):
        run_build(tmp_path, payment_verified=False, dry_run=True)


def test_build_proceeds_once_paid(tmp_path):
    assert run_build(tmp_path, payment_verified=True, dry_run=True).status == "skipped"


@pytest.mark.parametrize(
    "package_name, expected",
    [
        ("com.example.fieldnotes", ("com.example", "fieldnotes")),
        ("io.acme.sub.widget", ("io.acme.sub", "widget")),
    ],
)
def test_application_id_split_preserves_the_prd_package_name(package_name, expected):
    """`flutter create` composes applicationId as <org>.<project-name>. Splitting
    this way reproduces the PRD's package_name exactly instead of letting it
    drift to <org>.<dart_package_name>, which differs whenever the app name and
    the package name are not the same word."""
    assert _split_application_id(package_name) == expected


def test_build_without_a_prd_cannot_scaffold(tmp_path):
    """Payment verified but no PRD: skip honestly rather than build the wrong id."""
    result = run_build(tmp_path, payment_verified=True, dry_run=False, prd=None)
    assert result.status == "skipped"
    assert "PRD" in result.detail


def test_packaging_failure_fails_the_build(prd, tmp_path, monkeypatch):
    """A failed APK build must not report `done` with an empty apk_path."""
    import src.graph.nodes as nodes

    monkeypatch.setattr(
        nodes, "run_build",
        lambda *a, **k: BuildResult("failed", "gradle exploded"),
    )
    paid = prd.model_copy(update={"x402_payment_verified": True})
    app = build_graph(TemplateGenerator(), StubAnalyzer(), dry_run=False)
    final = app.invoke(initial_state(paid.model_dump(mode="json"), str(tmp_path)))

    assert final["phase"] == "failed"
    assert "gradle exploded" in final["failure"]


def test_unpaid_prd_still_produces_green_code(prd, tmp_path):
    assert prd.x402_payment_verified is False
    final = run(prd, tmp_path)
    assert final["phase"] == "done"
    assert any("gate closed" in line for line in final["log"])


def test_paid_prd_reaches_the_build_step(prd, tmp_path):
    paid = prd.model_copy(update={"x402_payment_verified": True})
    final = run(paid, tmp_path)
    assert any("packaging: skipped" in line for line in final["log"])


# --------------------------------------------------------------------------- #
# Lane violations short-circuit the graph
# --------------------------------------------------------------------------- #


class RogueGenerator(TemplateGenerator):
    """A GenUI subagent that tries to write business logic."""

    def build_ui(self, prd, design_md, diagnostics=None):
        files = super().build_ui(prd, design_md, diagnostics)
        files["lib/providers/sneaky.dart"] = "// should never land"
        return files


def test_out_of_lane_write_is_dropped_and_reported(prd, tmp_path):
    """A lane violation is repairable, not instantly fatal.

    The stray file is never written; the offending agent is told and gets repair
    passes to stop emitting it. A generator that keeps doing it exhausts the
    budget and the build stays red — but one that corrects itself recovers,
    which failing outright made impossible.
    """
    app = build_graph(RogueGenerator(), StubAnalyzer(), max_repairs=1, dry_run=True)
    final = app.invoke(initial_state(prd.model_dump(mode="json"), str(tmp_path)))

    assert not (tmp_path / "lib/providers/sneaky.dart").exists(), "must never land"
    assert "genui_lane_violation" in {d.code for d in final["diagnostics"]}
    assert any("out-of-lane" in line for line in final["log"])


# --------------------------------------------------------------------------- #
# Planning output
# --------------------------------------------------------------------------- #


def test_plan_emits_required_pubspec_keys(prd):
    plan: Plan = TemplateGenerator().plan(prd)
    for key in ("name:", "environment:", "dependencies:", "flutter_riverpod:"):
        assert key in plan.pubspec
    assert "Field Notes" in plan.design_md
    assert "lib/providers/**" in plan.design_md


def test_pubspec_is_valid_yaml_despite_colons_in_description():
    """Regression: an unquoted colon crashed the flutter tool before analysis."""
    prd = PRD.model_validate({
        "app_name": "Tricky",
        "package_name": "com.example.tricky",
        "description": "An app: it does things; it's \"quoted\" & 'awkward'",
        "screens": [{"id": "home", "title": "Home", "kind": "settings"}],
    })
    parsed = yaml.safe_load(TemplateGenerator().plan(prd).pubspec)

    assert parsed["description"] == "An app: it does things; it's \"quoted\" & 'awkward'"
    assert parsed["name"] == "tricky"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Ana's", "Ana\\'s"),
        ("$5", "\\$5"),
        ("back\\slash", "back\\\\slash"),
        ("a\nb", "a\\nb"),
        ("plain", "plain"),
    ],
)
def test_dart_string_escaping(raw, expected):
    assert dart_string(raw) == expected


def test_tricky_prd_emits_parseable_dart_literals():
    """Regression: an apostrophe in a screen title closed the Dart string early,
    turning the rest of the line into code (59 analyzer errors)."""
    prd = load_prd("evals/prds/tricky_strings.prd.json")
    ui = TemplateGenerator().build_ui(prd, "", [])

    for path, body in ui.items():
        for literal in re.findall(r"'((?:[^'\\\n]|\\.)*)'", body):
            # Every raw apostrophe and dollar inside a literal must be escaped.
            assert "'" not in literal.replace("\\'", ""), f"{path}: {literal}"
            assert "$" not in literal.replace("\\$", "").replace("$error", ""), (
                f"{path}: unescaped interpolation in {literal}"
            )


def test_cupertino_theme_is_honoured():
    """Was a silent no-op: the schema accepted `cupertino` and always emitted
    MaterialApp. The analyzer could never catch it — the code compiled fine."""
    prd = load_prd("evals/prds/cupertino.prd.json")
    ui = TemplateGenerator().build_ui(prd, "", [])

    assert "CupertinoApp" in ui["lib/ui/app.dart"]
    assert "MaterialApp" not in ui["lib/ui/app.dart"]
    # A CupertinoApp provides no MaterialLocalizations, so Material widgets
    # inside one throw at runtime. Swapping the root widget alone is not enough.
    for path, body in ui.items():
        assert "package:flutter/material.dart" not in body, path
        # Word-boundary anchored: CupertinoPageScaffold legitimately ends in
        # "Scaffold", and CupertinoListTile in "ListTile".
        for material_only in ("Scaffold", "AppBar", "ListTile", "TextFormField"):
            assert not re.search(rf"\b{material_only}\(", body), \
                f"{path} uses Material-only {material_only}"


def test_material_theme_stays_material():
    ui = TemplateGenerator().build_ui(load_prd(PRD_PATH), "", [])
    assert "MaterialApp" in ui["lib/ui/app.dart"]
    assert "CupertinoApp" not in ui["lib/ui/app.dart"]


def test_conformance_catches_theme_mismatch():
    """The check that made the cupertino gap visible in the first place."""
    prd = load_prd("evals/prds/cupertino.prd.json")
    material_output = {"lib/ui/app.dart": "return MaterialApp(title: 'x');"}

    codes = {d.code for d in check_conformance(prd, material_output)}
    assert "theme_mismatch" in codes


def test_conformance_catches_dropped_screen_and_model():
    prd = load_prd(PRD_PATH)
    codes = {d.code for d in check_conformance(prd, {"lib/ui/app.dart": "MaterialApp()"})}

    assert "missing_screen" in codes
    assert "missing_provider" in codes


def test_conformance_passes_on_real_generated_output():
    """No false positives against the generator's own output."""
    for name in ["examples/todo_app.prd.json", "evals/prds/cupertino.prd.json",
                 "evals/prds/multi_model.prd.json", "evals/prds/all_screen_kinds.prd.json"]:
        prd = load_prd(name)
        gen = TemplateGenerator()
        ui = gen.build_ui(prd, "", [])
        logic = gen.wire_logic(prd, "", ui, [])
        plan = gen.plan(prd)
        files = {**ui, **logic, "pubspec.yaml": plan.pubspec,
                 "analysis_options.yaml": plan.analysis_options}
        assert check_conformance(prd, files) == [], name


def test_model_written_pubspec_description_is_repaired():
    """A live model wrote `description: Auth-first app: sign in, browse.` — the
    same unquoted-colon break our own templates hit in Phase 1. The manifest is
    frozen planning output, so nothing downstream could repair it; punctuation
    is not design, so we repair it ourselves."""
    broken = (
        "name: gated\n"
        "description: Auth-first app: sign in, browse.\n"
        "version: 1.0.0\n"
    )
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(broken)

    parsed = yaml.safe_load(repair_pubspec_description(broken))
    assert parsed["description"] == "Auth-first app: sign in, browse."
    assert parsed["name"] == "gated"


def test_stray_prose_in_a_model_written_pubspec_is_commented_out():
    """The failure that cost a real mainnet purchase.

    Sonnet wrote an explanatory line flush-left with no `#`. At column 1 with a
    colon in it, YAML reads it as a new top-level key mid-document and aborts —
    "expected <document start>, but found <block mapping start>", the exact
    message the live build reported at line 9. `unparseable_pubspec` routes
    `fatal` because the manifest is frozen after planning, so the buyer paid and
    got zero repair attempts.
    """
    broken = (
        "name: field_notes\n"
        "description: A notes app.\n"
        "version: 1.0.0+1\n"
        "\n"
        "environment:\n"
        "  sdk: '>=3.0.0 <4.0.0'\n"
        "\n"
        "dependencies:\n"
        "Firestore collection: `observations`\n"
        "  flutter:\n"
        "    sdk: flutter\n"
    )
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(broken)

    parsed = yaml.safe_load(repair_stray_prose_line(broken))
    assert parsed["name"] == "field_notes"
    assert "flutter" in parsed["dependencies"]
    # The prose survives as a comment rather than being deleted: a human reading
    # the generated manifest should still see what the model meant.
    assert "# Firestore collection" in repair_stray_prose_line(broken)


def test_stray_prose_repair_never_touches_a_valid_pubspec():
    """It must be inert on healthy input — it runs on every build, and silently
    commenting out a legitimate top-level key would break working manifests."""
    healthy = (
        "name: ok\n"
        "description: Fine.\n"
        "version: 1.0.0\n"
        "environment:\n"
        "  sdk: '>=3.0.0 <4.0.0'\n"
        "dependencies:\n"
        "  flutter:\n"
        "    sdk: flutter\n"
        "flutter:\n"
        "  uses-material-design: true\n"
    )
    assert repair_stray_prose_line(healthy) == healthy


def test_pubspec_repair_gives_up_on_other_breakage():
    """Narrow by design: anything but the description is conformance's problem."""
    other = "name: x\ndeps: [unclosed\n  nested: {\n"
    assert repair_pubspec_description(other) == other


def test_smoke_tests_use_the_pubspec_package_name_not_the_prd():
    """A generator that names its package anything other than snake(app_name)
    made every `package:` import in the generated tests unresolvable."""
    prd = load_prd(PRD_PATH)
    gen = TemplateGenerator()
    ui = gen.build_ui(prd, "", [])
    files = {**ui, **gen.wire_logic(prd, "", ui, []), "pubspec.yaml": "name: renamed_pkg\n"}

    body = next(iter(build_smoke_tests(prd, files).values()))
    assert "package:renamed_pkg/" in body
    assert "package:field_notes/" not in body


def test_plan_enables_the_linter():
    """flutter_lints in dev_dependencies does nothing without this file."""
    plan = TemplateGenerator().plan(load_prd(PRD_PATH))
    assert "package:flutter_lints/flutter.yaml" in plan.analysis_options


# --------------------------------------------------------------------------- #
# Real-analyzer output parsing (no SDK required)
# --------------------------------------------------------------------------- #


def test_parses_flutter_analyze_text_output(tmp_path):
    """Flutter 3.44 dropped machine-readable output, so we parse the text."""
    line = (
        "  error - A value of type 'String' can't be returned from the function "
        "'_broken' because it has a return type of 'int' - "
        "lib\\ui\\settings_screen.dart:21:24 - return_of_invalid_type"
    )
    d = FlutterAnalyzer._parse_line(line, tmp_path)

    assert d is not None
    assert d.severity == "error"
    assert d.file == "lib/ui/settings_screen.dart"
    assert d.line == 21
    assert d.code == "return_of_invalid_type"
    assert "can't be returned" in d.message


def test_parses_warning_and_ignores_summary_lines(tmp_path):
    warning = (
        "warning - The declaration '_broken' isn't referenced - "
        "lib\\ui\\settings_screen.dart:21:5 - unused_element"
    )
    assert FlutterAnalyzer._parse_line(warning, tmp_path).severity == "warning"

    for noise in ["Analyzing current_build...", "2 issues found. (ran in 5.6s)", ""]:
        assert FlutterAnalyzer._parse_line(noise, tmp_path) is None


def test_real_analyzer_diagnostics_route_to_the_right_agent(tmp_path):
    """A real dart error code has no explicit mapping — path decides."""
    ui = FlutterAnalyzer._parse_line(
        "  error - bad - lib\\ui\\home_screen.dart:3:1 - return_of_invalid_type", tmp_path
    )
    logic = FlutterAnalyzer._parse_line(
        "  error - bad - lib\\providers\\x_providers.dart:3:1 - undefined_identifier",
        tmp_path,
    )
    assert owner_of(ui) == "genui"
    assert owner_of(logic) == "logic"
