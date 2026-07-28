"""Code-generation port.

One Protocol, three methods — one per productive subagent in CLAUDE.md §1. The
QA subagent is not here; it consumes `DartAnalyzer` instead.

Two implementations exist: `TemplateGenerator` (deterministic, offline, no
credentials) and `AnthropicGenerator` (real Claude calls). The graph never knows
which it has.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.ports.analyzer import Diagnostic
from src.prd.schema import PRD


@dataclass(frozen=True)
class Plan:
    design_md: str
    pubspec: str
    analysis_options: str = ""


class CodeGenerator(Protocol):
    def plan(self, prd: PRD) -> Plan:
        """Planning subagent: draft DESIGN.md and pubspec.yaml."""
        ...

    def build_ui(
        self, prd: PRD, design_md: str, diagnostics: list[Diagnostic]
    ) -> dict[str, str]:
        """GenUI subagent: the widget tree only. Returns lib/ui/** paths.

        `diagnostics` is non-empty on a repair pass and contains only the
        UI-owned subset — patch what they name, don't regenerate blindly.
        """
        ...

    def wire_logic(
        self,
        prd: PRD,
        design_md: str,
        ui_files: dict[str, str],
        diagnostics: list[Diagnostic],
    ) -> dict[str, str]:
        """Logic subagent: Riverpod + Firebase. Returns lib/providers/** and main.dart.

        When `diagnostics` is non-empty this is a repair pass — patch the specific
        callbacks named in the diagnostics rather than regenerating everything
        (CLAUDE.md §4, Dart MCP Desync).
        """
        ...


def get_generator(kind: str, **kwargs) -> CodeGenerator:
    if kind == "template":
        from src.ports.templates import TemplateGenerator

        return TemplateGenerator(**kwargs)
    if kind == "claude":
        from src.ports.llm import AnthropicGenerator

        return AnthropicGenerator(**kwargs)
    raise ValueError(f"unknown generator {kind!r}; expected 'template' or 'claude'")
