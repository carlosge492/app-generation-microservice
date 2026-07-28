from __future__ import annotations

import json

import pytest

from src.build.pipeline import run_build
from src.graph.builder import build_graph
from src.graph.state import initial_state
from src.payments.x402 import PaymentNotVerified, is_verified
from src.ports.analyzer import StubAnalyzer
from src.ports.generator import Plan
from src.ports.templates import TemplateGenerator
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
    final = run(prd, tmp_path, fault="undefined_provider")

    assert final["phase"] == "done", final.get("failure")
    assert final["diagnostics"] == []
    assert final["repair_attempts"] == 1
    assert (tmp_path / "lib/providers/repair_providers.dart").exists()
    assert any("repair pass" in line for line in final["log"])


def test_repair_budget_is_enforced(prd, tmp_path):
    final = run(prd, tmp_path, fault="undefined_provider", max_repairs=0)

    assert final["diagnostics"], "expected the build to stay red"
    assert final["repair_attempts"] == 0


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

    def build_ui(self, prd, design_md):
        files = super().build_ui(prd, design_md)
        files["lib/providers/sneaky.dart"] = "// should never land"
        return files


def test_out_of_lane_write_fails_the_build(prd, tmp_path):
    app = build_graph(RogueGenerator(), StubAnalyzer(), dry_run=True)
    final = app.invoke(initial_state(prd.model_dump(mode="json"), str(tmp_path)))

    assert final["phase"] == "failed"
    assert "lib/providers/sneaky.dart" in final["failure"]
    assert not (tmp_path / "lib/providers/sneaky.dart").exists()


# --------------------------------------------------------------------------- #
# Planning output
# --------------------------------------------------------------------------- #


def test_plan_emits_required_pubspec_keys(prd):
    plan: Plan = TemplateGenerator().plan(prd)
    for key in ("name:", "environment:", "dependencies:", "flutter_riverpod:"):
        assert key in plan.pubspec
    assert "Field Notes" in plan.design_md
    assert "lib/providers/**" in plan.design_md
