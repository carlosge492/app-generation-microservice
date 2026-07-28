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
from src.ports.analyzer import DartAnalyzer, ToolchainUnavailable
from src.ports.generator import CodeGenerator
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
            "phase": "genui",
            "log": [f"planning: DESIGN.md ({len(plan.design_md)} chars) + pubspec.yaml drafted"],
        }

    return planning


def make_genui_node(generator: CodeGenerator) -> Node:
    def genui(state: BuildState) -> dict[str, Any]:
        prd = _prd(state)
        files = generator.build_ui(prd, state["design_md"])
        stray = [p for p in files if not p.startswith("lib/ui/")]
        if stray:
            return {
                "phase": "failed",
                "failure": f"GenUI subagent wrote outside lib/ui/: {stray}",
                "log": [f"genui: REJECTED {len(stray)} out-of-lane file(s)"],
            }
        return {
            "ui_files": files,
            "phase": "logic",
            "log": [f"genui: {len(files)} widget file(s) written"],
        }

    return genui


def make_logic_node(generator: CodeGenerator) -> Node:
    def logic(state: BuildState) -> dict[str, Any]:
        prd = _prd(state)
        diagnostics = state.get("diagnostics", [])
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
            f"logic: repair pass #{state.get('repair_attempts', 0) + 1} "
            f"against {len(diagnostics)} diagnostic(s)"
            if repairing
            else f"logic: {len(files)} state file(s) written"
        )
        return {
            "provider_files": merged,
            "repair_attempts": state.get("repair_attempts", 0) + (1 if repairing else 0),
            "phase": "qa",
            "log": [note],
        }

    return logic


def make_qa_node(analyzer: DartAnalyzer) -> Node:
    def qa(state: BuildState) -> dict[str, Any]:
        build_dir = Path(state["build_dir"])
        files = all_files(state)

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

        errors = [d for d in diagnostics if d.severity == "error"]
        return {
            "diagnostics": errors,
            "phase": "packaging" if not errors else "logic",
            "log": [
                f"qa: {len(files)} file(s) analysed, "
                f"{len(errors)} error(s), {len(diagnostics) - len(errors)} non-fatal"
            ],
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
        if not state.get("diagnostics"):
            return "package"
        if state.get("repair_attempts", 0) >= max_repairs:
            return "fail"
        return "repair"

    return route_after_qa


def route_after_agent(state: BuildState) -> str:
    """Short-circuit the graph when a subagent violated its lane."""
    return "fail" if state.get("phase") == "failed" else "continue"
