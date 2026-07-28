"""Shared state for the build loop.

The state doubles as the enforcement point for CLAUDE.md's separation of concerns:
`ui_files` is writable only by the GenUI subagent, `provider_files` only by the
Logic subagent. Nothing merges them into one bucket.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from src.ports.analyzer import Diagnostic

Phase = Literal["planning", "genui", "logic", "qa", "packaging", "done", "failed"]


class BuildState(TypedDict, total=False):
    # --- inputs, never mutated after the planning phase ---
    prd: dict[str, Any]
    build_dir: str

    # --- planning subagent output ---
    design_md: str
    pubspec: str
    analysis_options: str

    # --- genui subagent output: lib/ui/** only ---
    ui_files: dict[str, str]

    # --- logic subagent output: lib/providers/** and lib/main.dart ---
    provider_files: dict[str, str]

    # --- qa subagent output ---
    test_files: dict[str, str]
    written_files: list[str]
    # Out-of-lane writes, recorded by the offending agent so the
    # loop can repair them instead of failing outright.
    genui_violations: list[Diagnostic]
    logic_violations: list[Diagnostic]
    diagnostics: list[Diagnostic]
    repair_attempts: int
    # Signature of the previous round's diagnostics, and whether this round is
    # identical to it. A stalled round means the owning agent had its turn and
    # changed nothing, so the router hands the problem to the other agent.
    diagnostic_signature: list[str]
    stalled: bool

    # --- packaging gate ---
    x402_payment_verified: bool
    apk_path: str

    phase: Phase
    failure: str
    log: Annotated[list[str], operator.add]


def initial_state(prd: dict[str, Any], build_dir: str) -> BuildState:
    return BuildState(
        prd=prd,
        build_dir=build_dir,
        ui_files={},
        provider_files={},
        diagnostics=[],
        repair_attempts=0,
        x402_payment_verified=bool(prd.get("x402_payment_verified", False)),
        phase="planning",
        log=[],
    )


def all_files(state: BuildState) -> dict[str, str]:
    """The full project tree as path -> content, ready to write to disk."""
    files: dict[str, str] = {}
    if state.get("pubspec"):
        files["pubspec.yaml"] = state["pubspec"]
    if state.get("analysis_options"):
        files["analysis_options.yaml"] = state["analysis_options"]
    if state.get("design_md"):
        files["DESIGN.md"] = state["design_md"]
    files.update(state.get("ui_files", {}))
    files.update(state.get("provider_files", {}))
    return files
