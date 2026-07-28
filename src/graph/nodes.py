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
    ToolchainUnavailable,
    is_fatal,
)
from src.ports.conformance import check_conformance
from src.ports.generator import CodeGenerator
from src.ports.ownership import owner_of, route_for
from src.ports.runtime import TestRunner
from src.ports.smoke import build_smoke_tests
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
            "pubspec": plan.pubspec,
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
        stray = [p for p in files if not p.startswith("lib/ui/")]
        if stray:
            return {
                "phase": "failed",
                "failure": f"GenUI subagent wrote outside lib/ui/: {stray}",
                "log": [f"genui: REJECTED {len(stray)} out-of-lane file(s)"],
            }

        merged = {**state.get("ui_files", {}), **files} if repairing else files
        note = (
            f"genui: repair pass against {len(mine)} UI diagnostic(s)"
            if repairing
            else f"genui: {len(files)} widget file(s) written"
        )
        return {"ui_files": merged, "phase": "logic", "log": [note]}

    return genui


def make_logic_node(generator: CodeGenerator) -> Node:
    def logic(state: BuildState) -> dict[str, Any]:
        prd = _prd(state)
        # Only the diagnostics this agent can actually act on.
        _all = state.get("diagnostics", [])
        diagnostics = _all if state.get("stalled") else [d for d in _all if owner_of(d) == "logic"]
        repairing = bool(diagnostics)

        files = generator.wire_logic(prd, state["design_md"], state["ui_files"], diagnostics)
        allowed = ("lib/providers/", "lib/main.dart")
        stray = [p for p in files if not p.startswith(allowed)]
        if stray:
            return {
                "phase": "failed",
                "failure": f"Logic subagent wrote outside its lane: {stray}",
                "log": [f"logic: REJECTED {len(stray)} out-of-lane file(s)"],
            }

        merged = {**state.get("provider_files", {}), **files} if repairing else files
        note = (
            f"logic: repair pass against {len(diagnostics)} state diagnostic(s)"
            if repairing
            else f"logic: {len(files)} state file(s) written"
        )
        return {"provider_files": merged, "phase": "qa", "log": [note]}

    return logic


def make_qa_node(analyzer: DartAnalyzer, runner: TestRunner | None = None) -> Node:
    def qa(state: BuildState) -> dict[str, Any]:
        build_dir = Path(state["build_dir"])
        files = all_files(state)

        # Smoke tests are derived from the generated sources, so they are built
        # here rather than by a subagent, and they ship with the app.
        smoke = build_smoke_tests(_prd(state), files)
        files.update(smoke)

        # Materialise the project, then analyse it the way the real pipeline would.
        for rel, content in files.items():
            target = build_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        try:
            diagnostics = analyzer.analyze(build_dir)
        except ToolchainUnavailable as exc:
            return {
                "phase": "failed",
                "failure": str(exc),
                "log": [f"qa: {exc}"],
            }

        # Static analysis grades syntax; conformance grades whether the app is
        # the one the PRD asked for. Neither subsumes the other.
        diagnostics = diagnostics + check_conformance(_prd(state), files)

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
            f"qa: {len(files)} file(s) analysed ({len(smoke)} smoke test(s)), "
            f"{len(errors)} error(s), "
            f"{len(diagnostics) - len(errors)} non-fatal"
        )
        if escalated:
            note += f" ({escalated} escalated as unwired/dead code)"
        if stalled:
            note += " [stalled — escalating to the other agent]"
        return {
            "diagnostics": errors,
            "diagnostic_signature": signature,
            "stalled": stalled,
            "test_files": smoke,
            "repair_attempts": attempts,
            "phase": "packaging" if not errors else "qa",
            "log": [note],
        }

    return qa


def make_packaging_node(dry_run: bool) -> Node:
    def packaging(state: BuildState) -> dict[str, Any]:
        try:
            result = run_build(
                Path(state["build_dir"]),
                payment_verified=state.get("x402_payment_verified", False),
                dry_run=dry_run,
            )
        except PaymentNotVerified as exc:
            # Not a build failure: the code is green, it just isn't paid for.
            return {
                "phase": "done",
                "log": [f"packaging: gate closed — {exc}"],
            }
        return {
            "phase": "done",
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
