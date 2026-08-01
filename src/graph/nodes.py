"""The four subagents from CLAUDE.md §1, plus the packaging gate.

Each node is a pure-ish function of state -> partial state update. Dependencies
(generator, analyzer) are injected so the graph can be exercised offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.build.pipeline import run_build
from src.graph.state import BuildState, all_files
from src.payments.x402 import PaymentNotVerified
from src.ports.analyzer import (
    HALLUCINATION_WARNINGS,
    DartAnalyzer,
    Diagnostic,
    ToolchainUnavailable,
    is_fatal,
)
from src.ports.conformance import check_conformance
from src.ports.generator import CodeGenerator
from src.ports.ownership import owner_of, route_for
from src.ports.runtime import TestRunner
from src.ports.navigation import build_navigation_test
from src.ports.pubspec import ensure_test_dependencies
from src.ports.roundtrip import build_roundtrip_tests
from src.ports.smoke import build_smoke_tests
from src.ports.templates import (
    ensure_lint_dependency,
    repair_pubspec_description,
    repair_stray_prose_line,
)
from src.prd.schema import PRD

Node = Callable[[BuildState], dict[str, Any]]


def _prd(state: BuildState) -> PRD:
    return PRD.model_validate(state["prd"])


def make_planning_node(generator: CodeGenerator) -> Node:
    def planning(state: BuildState) -> dict[str, Any]:
        prd = _prd(state)
        plan = generator.plan(prd)
        return {
            "design_md": plan.design_md,
            # Infrastructure we own, like analysis_options.yaml itself.
            # Both repairs are no-ops on a manifest that already parses, and
            # each keeps its change only if it produces valid YAML. They run
            # here because the manifest is frozen once GenUI starts: an
            # unparseable pubspec is routed `fatal`, so a buyer who hits one has
            # already paid for a build that will never get a repair pass.
            "pubspec": ensure_lint_dependency(
                repair_stray_prose_line(repair_pubspec_description(plan.pubspec))
            ),
            "analysis_options": plan.analysis_options,
            "phase": "genui",
            "log": [f"planning: DESIGN.md ({len(plan.design_md)} chars) + pubspec.yaml drafted"],
        }

    return planning


def make_genui_node(generator: CodeGenerator) -> Node:
    def genui(state: BuildState) -> dict[str, Any]:
        prd = _prd(state)
        # Only the diagnostics this agent can actually act on.
        diags = state.get("diagnostics", [])
        # On a stalled round this agent is the escalation target, so it takes
        # the whole batch rather than only what it nominally owns.
        mine = diags if state.get("stalled") else [d for d in diags if owner_of(d) == "genui"]
        repairing = bool(mine)

        files = generator.build_ui(prd, state["design_md"], mine)

        # Out-of-lane files are dropped, never written — but the agent is told,
        # so the loop can repair it. Failing the build outright wasted every
        # remaining repair pass on a mistake the offender could simply undo,
        # and dropping them silently (as `_bundle` once did) hid the problem
        # entirely. Drop *and* report.
        kept = {p: c for p, c in files.items() if p.startswith("lib/ui/")}
        violations = [
            Diagnostic(
                "error", path, 0, "genui_lane_violation",
                f"GenUI wrote {path!r}, which is outside lib/ui/. Everything else "
                f"under lib/ belongs to the Logic subagent; emit widgets only.",
            )
            for path in sorted(set(files) - set(kept))
        ]

        merged = {**state.get("ui_files", {}), **kept} if repairing else kept
        note = (
            f"genui: repair pass against {len(mine)} UI diagnostic(s)"
            if repairing
            else f"genui: {len(kept)} widget file(s) written"
        )
        if violations:
            note += f" ({len(violations)} out-of-lane file(s) dropped)"
        return {
            "ui_files": merged,
            "genui_violations": violations,
            "phase": "logic",
            "log": [note],
        }

    return genui


def make_logic_node(generator: CodeGenerator) -> Node:
    def logic(state: BuildState) -> dict[str, Any]:
        prd = _prd(state)
        # Only the diagnostics this agent can actually act on.
        _all = state.get("diagnostics", [])
        diagnostics = _all if state.get("stalled") else [d for d in _all if owner_of(d) == "logic"]
        repairing = bool(diagnostics)

        files = generator.wire_logic(prd, state["design_md"], state["ui_files"], diagnostics)
        # Logic owns everything under lib/ except the widget tree. Enumerating
        # allowed directories instead assumed TemplateGenerator's layout, where
        # models live inside provider files; a generator following ordinary
        # Flutter convention writes lib/models/, lib/services/ and the
        # flutterfire-generated lib/firebase_options.dart, none of which are UI.
        # The invariant that matters is that UI and state never mix.
        kept = {
            p: c for p, c in files.items()
            if p.startswith("lib/") and not p.startswith("lib/ui/")
        }
        violations = [
            Diagnostic(
                "error", path, 0, "logic_lane_violation",
                f"Logic wrote {path!r}. The widget tree under lib/ui/ belongs to "
                f"the GenUI subagent and has already been written; own state, "
                f"models and the composition root instead.",
            )
            for path in sorted(set(files) - set(kept))
        ]

        merged = {**state.get("provider_files", {}), **kept} if repairing else kept
        note = (
            f"logic: repair pass against {len(diagnostics)} state diagnostic(s)"
            if repairing
            else f"logic: {len(kept)} state file(s) written"
        )
        if violations:
            note += f" ({len(violations)} out-of-lane file(s) dropped)"
        return {
            "provider_files": merged,
            "logic_violations": violations,
            "phase": "qa",
            "log": [note],
        }

    return logic


def make_qa_node(analyzer: DartAnalyzer, runner: TestRunner | None = None) -> Node:
    def qa(state: BuildState) -> dict[str, Any]:
        build_dir = Path(state["build_dir"])
        files = all_files(state)

        # Smoke tests are derived from the generated sources, so they are built
        # here rather than by a subagent, and they ship with the app.
        smoke = build_smoke_tests(_prd(state), files)
        files.update(smoke)

        # Round-trip tests ship with the app but are not run here: they need a
        # Firestore emulator and a browser, which the analysis loop deliberately
        # does not depend on. Generating them anyway means the app carries the
        # one check that catches wire-type bugs, for whoever can run it.
        roundtrip = build_roundtrip_tests(_prd(state), files)
        files.update(roundtrip)
        navigation = build_navigation_test(_prd(state), files)
        files.update(navigation)

        # Those tests import `package:integration_test/...`, which resolves only
        # if the pubspec declares it. TemplateGenerator's template happens to;
        # nothing ever told any other generator to, because the requirement
        # comes from files written here rather than from the PRD. Without this
        # the harness's own output fails analysis, and the resulting diagnostics
        # are charged to the generated app — spending repair attempts on code
        # that was never wrong.
        if (roundtrip or navigation) and files.get("pubspec.yaml"):
            files["pubspec.yaml"] = ensure_test_dependencies(files["pubspec.yaml"])

        # Remove anything we wrote on a previous pass that is no longer part of
        # the app. A repair pass that renames a screen used to leave the old
        # file — and its now-dangling smoke test — on disk, which then failed
        # analysis for a file the generator had correctly deleted.
        for stale in sorted(state.get("written_files", [])):
            if stale not in files:
                (build_dir / stale).unlink(missing_ok=True)

        # Materialise the project, then analyse it the way the real pipeline would.
        for rel, content in files.items():
            target = build_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        # Conformance first: it is toolchain-free and catches an unparseable
        # pubspec, which makes `flutter analyze` abort with a YAML stack trace
        # instead of a diagnostic. Running the analyzer on a project we already
        # know is malformed only buries the real cause.
        conformance = check_conformance(_prd(state), files)
        pubspec_broken = any(d.code == "unparseable_pubspec" for d in conformance)

        diagnostics: list[Diagnostic] = []
        if not pubspec_broken:
            try:
                diagnostics = analyzer.analyze(build_dir)
            except ToolchainUnavailable as exc:
                return {
                    "phase": "failed",
                    "failure": str(exc),
                    "log": [f"qa: {exc}"],
                }

        # Static analysis grades syntax; conformance grades whether the app is
        # the one the PRD asked for; lane violations were recorded by the agents
        # themselves. None subsumes the others.
        diagnostics = (
            diagnostics
            + conformance
            + state.get("genui_violations", [])
            + state.get("logic_violations", [])
        )

        # Static analysis proves it compiles; the widget tests prove it runs.
        if runner is not None:
            try:
                diagnostics = diagnostics + runner.run(build_dir)
            except ToolchainUnavailable as exc:
                return {"phase": "failed", "failure": str(exc), "log": [f"qa: {exc}"]}

        errors = [d for d in diagnostics if is_fatal(d)]
        escalated = sum(
            1 for d in errors
            if d.severity != "error" and d.code in HALLUCINATION_WARNINGS
        )
        # The repair counter lives here so it ticks once per failed analysis,
        # regardless of how many agents the router then involves.
        attempts = state.get("repair_attempts", 0) + (1 if errors else 0)

        # Identical diagnostics two rounds running means the last repair pass
        # accomplished nothing.
        signature = sorted(f"{d.code}:{d.file}:{d.line}" for d in errors)
        stalled = bool(errors) and signature == state.get("diagnostic_signature")
        note = (
            f"qa: {len(files)} file(s) analysed "
            f"({len(smoke)} smoke test(s), "
            f"{sum(1 for p in roundtrip if p.endswith('_roundtrip_test.dart'))} "
            f"round-trip test(s), "
            f"{sum(1 for p in navigation if p.endswith('navigation_test.dart'))} "
            f"navigation test(s)), "
            f"{len(errors)} error(s), "
            f"{len(diagnostics) - len(errors)} non-fatal"
        )
        if escalated:
            note += f" ({escalated} escalated as unwired/dead code)"
        if stalled:
            note += " [stalled — escalating to the other agent]"
        return {
            "diagnostics": errors,
            "written_files": sorted(files),
            "diagnostic_signature": signature,
            "stalled": stalled,
            "test_files": smoke,
            "repair_attempts": attempts,
            "phase": "packaging" if not errors else "qa",
            "log": [note],
        }

    return qa


def make_packaging_node(
    dry_run: bool,
    flutter_root: str | None = None,
    build_mode: str = "debug",
    sdk_root: str | None = None,
) -> Node:
    def packaging(state: BuildState) -> dict[str, Any]:
        try:
            result = run_build(
                Path(state["build_dir"]),
                payment_verified=state.get("x402_payment_verified", False),
                prd=_prd(state),
                flutter_root=flutter_root,
                dry_run=dry_run,
                build_mode=build_mode,
                sdk_root=sdk_root,
            )
        except PaymentNotVerified as exc:
            # Not a build failure: the code is green, it just isn't paid for.
            return {
                "phase": "done",
                "log": [f"packaging: gate closed — {exc}"],
            }
        return {
            "phase": "failed" if result.status == "failed" else "done",
            "failure": result.detail if result.status == "failed" else "",
            "apk_path": result.apk_path or "",
            "log": [f"packaging: {result.status} — {result.detail}"],
        }

    return packaging


def make_router(max_repairs: int) -> Callable[[BuildState], str]:
    def route_after_qa(state: BuildState) -> str:
        if state.get("phase") == "failed":
            return "fail"
        diagnostics = state.get("diagnostics", [])
        if not diagnostics:
            return "package"
        if state.get("repair_attempts", 0) > max_repairs:
            return "fail"
        # Send the batch to whichever agent owns it; upstream-owned diagnostics
        # (frozen pubspec/DESIGN.md) are unfixable and fail fast.
        target = route_for(diagnostics)
        if state.get("stalled") and target in ("repair_ui", "repair_logic"):
            # The owner had its turn and changed nothing. Rather than spend the
            # rest of the budget re-running it, give the other agent the problem:
            # a provider the PRD does not justify is fixed by the UI dropping the
            # reference, not by Logic inventing something to satisfy it.
            return "repair_logic" if target == "repair_ui" else "repair_ui"
        return target

    return route_after_qa


def route_after_agent(state: BuildState) -> str:
    """Short-circuit the graph when a subagent violated its lane."""
    return "fail" if state.get("phase") == "failed" else "continue"
