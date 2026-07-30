"""QA/compiler port.

`DartAnalyzer` is the seam between the agent loop and the Flutter toolchain.
`StubAnalyzer` runs today with no SDK installed; `DartSdkAnalyzer` shells out to
the real `dart analyze` once Flutter is on PATH. Swapping one for the other must
not require touching any node.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.ports.toolchain import sdk_executable

import yaml


class ToolchainUnavailable(RuntimeError):
    """Raised when a real toolchain is requested but not installed."""


@dataclass(frozen=True)
class Diagnostic:
    severity: str  # "error" | "warning" | "info"
    file: str
    line: int
    code: str
    message: str

    def render(self) -> str:
        return f"{self.severity.upper()} {self.file}:{self.line} [{self.code}] {self.message}"


class DartAnalyzer(Protocol):
    def analyze(self, project_dir: Path) -> list[Diagnostic]: ...


# Warnings the QA subagent promotes to build-breaking errors.
#
# In a hand-written codebase an orphaned declaration is untidiness. In a
# generated one it is evidence of a hallucination: the model decided it needed a
# helper, wrote it, and never wired it to anything. The unreferenced code is the
# visible half of the mistake — the missing call site is the half that matters,
# and it is exactly the kind of silent incompleteness a buyer would discover at
# runtime. Fail the build and make the agent finish the job.
#
# Escalated here rather than only in analysis_options.yaml so the policy holds
# even when the analyzer config is missing, overridden, or written by a model.
HALLUCINATION_WARNINGS = frozenset({
    "unused_element",         # private declaration nothing references
    "unused_field",           # field written but never read
    "unused_local_variable",  # computed, then dropped
    "dead_code",              # unreachable branch
})


# Warnings that mean the analysis itself is not trustworthy.
#
# `include_file_not_found` fires when analysis_options.yaml includes a lint
# package the pubspec never declared. The analyser then runs with default rules
# only, so every guarantee built on the lint set — unused code, dead code — is
# silently void while the build still reports green. A check that has quietly
# stopped running is worse than one that never existed, so this is fatal.
ANALYSIS_INTEGRITY_WARNINGS = frozenset({
    "include_file_not_found",
    "analysis_option_unsupported_option",
})


def is_fatal(diagnostic: Diagnostic) -> bool:
    return (
        diagnostic.severity == "error"
        or diagnostic.code in HALLUCINATION_WARNINGS
        or diagnostic.code in ANALYSIS_INTEGRITY_WARNINGS
    )


# --------------------------------------------------------------------------- #
# Stub implementation
# --------------------------------------------------------------------------- #

_SETSTATE = re.compile(r"\bsetState\s*\(")
_ANIMATION_EXEMPT = re.compile(r"//\s*localized animation toggle")
_FORBIDDEN_IN_UI = re.compile(r"import\s+'package:(firebase_\w+|cloud_firestore)/")
_WIDGET_BUILD = re.compile(r"\bWidget\s+build\s*\(")
# Trailing `[.)]` so `ref.read(fooProvider.notifier)` is checked too, not just
# the bare `ref.watch(fooProvider)` form.
_PROVIDER_REF = re.compile(r"\bref\.(?:watch|read)\(\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*[.)]")
_PROVIDER_DECL = re.compile(r"^final\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*\w*Provider", re.M)


class StubAnalyzer:
    """Toolchain-free static analysis.

    Deliberately not a no-op: it enforces the conventions in CLAUDE.md §3 and
    catches the provider-desync class of bug described in §4, so the repair loop
    is exercised end to end before Flutter is ever installed.
    """

    def analyze(self, project_dir: Path) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        out += self._check_pubspec(project_dir)

        ui = sorted((project_dir / "lib" / "ui").rglob("*.dart"))
        providers = sorted((project_dir / "lib" / "providers").rglob("*.dart"))

        declared = self._declared_providers(providers)
        for path in ui:
            out += self._check_ui_file(project_dir, path, declared)
        for path in providers:
            out += self._check_provider_file(project_dir, path)

        for path in [*ui, *providers, *sorted((project_dir / "lib").glob("*.dart"))]:
            out += self._check_braces(project_dir, path)

        return out

    # -- individual rules -------------------------------------------------- #

    def _check_pubspec(self, root: Path) -> list[Diagnostic]:
        pubspec = root / "pubspec.yaml"
        if not pubspec.exists():
            return [Diagnostic("error", "pubspec.yaml", 0, "missing_pubspec",
                               "pubspec.yaml was never written by the planning subagent")]
        text = pubspec.read_text(encoding="utf-8")

        # Parse, don't just grep. An unquoted colon in a PRD-supplied description
        # yields a file that contains every required key and still crashes the
        # flutter tool before analysis begins.
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            detail = " ".join(str(exc).split())
            return [Diagnostic("error", "pubspec.yaml", 0, "unparseable_pubspec",
                               f"pubspec.yaml is not valid YAML: {detail}")]
        if not isinstance(parsed, dict):
            return [Diagnostic("error", "pubspec.yaml", 0, "unparseable_pubspec",
                               "pubspec.yaml did not parse to a YAML mapping")]

        return [
            Diagnostic("error", "pubspec.yaml", 0, "incomplete_pubspec",
                       f"pubspec.yaml is missing required key {key!r}")
            for key in ("name", "environment", "dependencies")
            if key not in parsed
        ]

    def _declared_providers(self, provider_files: list[Path]) -> set[str]:
        declared: set[str] = set()
        for path in provider_files:
            declared |= set(_PROVIDER_DECL.findall(path.read_text(encoding="utf-8")))
        return declared

    def _check_ui_file(self, root: Path, path: Path, declared: set[str]) -> list[Diagnostic]:
        rel = path.relative_to(root).as_posix()
        out: list[Diagnostic] = []
        lines = path.read_text(encoding="utf-8").splitlines()

        for n, line in enumerate(lines, start=1):
            if _SETSTATE.search(line) and not _ANIMATION_EXEMPT.search(line):
                out.append(Diagnostic(
                    "error", rel, n, "setstate_in_ui",
                    "setState is banned outside a localized animation toggle; use Riverpod "
                    "(mark the exempt line with `// localized animation toggle`)",
                ))
            if _FORBIDDEN_IN_UI.search(line):
                out.append(Diagnostic(
                    "error", rel, n, "logic_in_ui",
                    "UI files must not import Firebase; that belongs in lib/providers/",
                ))

        # The provider-desync check: a widget watching a provider the Logic
        # subagent never declared is exactly the "Method not found" failure.
        body = "\n".join(lines)
        for name in sorted(set(_PROVIDER_REF.findall(body))):
            if name not in declared:
                line_no = next(
                    (n for n, ln in enumerate(lines, 1) if f"({name})" in ln), 1
                )
                out.append(Diagnostic(
                    "error", rel, line_no, "undefined_provider",
                    f"provider {name!r} is watched here but never declared in lib/providers/",
                ))
        return out

    def _check_provider_file(self, root: Path, path: Path) -> list[Diagnostic]:
        rel = path.relative_to(root).as_posix()
        out: list[Diagnostic] = []
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _WIDGET_BUILD.search(line):
                out.append(Diagnostic(
                    "error", rel, n, "ui_in_logic",
                    "state files must not build widgets; that belongs in lib/ui/",
                ))
        return out

    def _check_braces(self, root: Path, path: Path) -> list[Diagnostic]:
        text = path.read_text(encoding="utf-8")
        depth = text.count("{") - text.count("}")
        if depth == 0:
            return []
        return [Diagnostic(
            "error", path.relative_to(root).as_posix(), 0, "unbalanced_braces",
            f"unbalanced braces ({depth:+d}); file is not parseable Dart",
        )]


# --------------------------------------------------------------------------- #
# Real implementation
# --------------------------------------------------------------------------- #

class FlutterAnalyzer:
    """The real QA subagent: `flutter analyze`.

    Neither `dart analyze` nor `flutter analyze` offers machine-readable output
    on Flutter 3.44 (`--machine` is gone, `--format=json` never existed on the
    Flutter wrapper), so we parse the text format:

        error - <message> - <file>:<line>:<col> - <code>

    `flutter analyze` runs `pub get` itself, which is the step that actually
    gates analysis: without a `.dart_tool/package_config.json` every
    `package:flutter/...` import is an unresolved-URI error and the real
    diagnostics are buried. Platform folders (android/, ios/) are NOT needed to
    analyse — they are a packaging concern.
    """

    # Trailing ` - ` separated fields, parsed right-to-left so a message
    # containing " - " does not corrupt the split.
    _SUMMARY = re.compile(r"^\d+ issue|^No issues|^Analyzing ")

    def __init__(self, flutter_root: str | Path | None = None) -> None:
        self.flutter_root = Path(flutter_root) if flutter_root else None

    def _tool(self, name: str) -> str:
        if self.flutter_root:
            tool = sdk_executable(self.flutter_root / "bin", name)
            if tool is not None:
                return tool
            raise ToolchainUnavailable(
                f"{name!r} not found under {self.flutter_root / 'bin'}"
            )
        found = shutil.which(name)
        if found is None:
            raise ToolchainUnavailable(
                f"{name!r} is not on PATH and no --flutter-root was given. Install the "
                f"Flutter SDK, or run with --analyzer stub."
            )
        return found

    def analyze(self, project_dir: Path) -> list[Diagnostic]:
        proc = subprocess.run(
            [self._tool("flutter"), "analyze", "--no-congratulate"],
            cwd=project_dir, capture_output=True, text=True, check=False,
            # Flutter emits UTF-8; Windows would otherwise decode with cp1252,
            # and a single undecodable byte kills subprocess's reader thread,
            # leaving stdout as None. That surfaced as
            # "TypeError: unsupported operand type(s) for +: 'NoneType' and
            # 'str'" five PRDs into a sweep, nowhere near the real cause.
            encoding="utf-8", errors="replace",
        )
        # 0 = clean, 1 = issues found. Anything else is the tool itself failing
        # (unresolvable dependencies, an unparseable pubspec, no network).
        if proc.returncode not in (0, 1):
            raise ToolchainUnavailable(
                f"`flutter analyze` failed ({proc.returncode}) in {project_dir}:\n"
                f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
            )

        out: list[Diagnostic] = []
        for line in (proc.stdout or "").splitlines():
            parsed = self._parse_line(line, project_dir)
            if parsed is not None:
                out.append(parsed)
        return out

    @classmethod
    def _parse_line(cls, line: str, project_dir: Path) -> Diagnostic | None:
        stripped = line.strip()
        if not stripped or cls._SUMMARY.match(stripped):
            return None
        # severity - message - file:line:col - code
        parts = stripped.rsplit(" - ", 2)
        if len(parts) != 3:
            return None
        head, location, code = parts
        severity, _, message = head.partition(" - ")
        severity = severity.strip().lower()
        if severity not in {"error", "warning", "info"}:
            return None

        file_part, _, line_col = location.rpartition(":")
        file_part, _, line_no = file_part.rpartition(":")
        if not file_part:
            file_part, line_no = location, "0"

        try:
            # Ownership routing keys off repo-relative POSIX paths.
            rel = Path(file_part).resolve().relative_to(project_dir.resolve()).as_posix()
        except (ValueError, OSError):
            rel = file_part.replace("\\", "/")

        return Diagnostic(
            severity=severity,
            file=rel,
            line=int(line_no) if line_no.isdigit() else 0,
            code=code.strip(),
            message=message.strip(),
        )


def get_analyzer(kind: str, flutter_root: str | Path | None = None) -> DartAnalyzer:
    if kind == "stub":
        return StubAnalyzer()
    if kind == "dart":
        return FlutterAnalyzer(flutter_root)
    raise ValueError(f"unknown analyzer {kind!r}; expected 'stub' or 'dart'")
