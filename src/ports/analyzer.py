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
        missing = [k for k in ("name:", "environment:", "dependencies:") if k not in text]
        return [
            Diagnostic("error", "pubspec.yaml", 0, "incomplete_pubspec",
                       f"pubspec.yaml is missing required key {k!r}")
            for k in missing
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

class DartSdkAnalyzer:
    """Shells out to `dart analyze --format=json`.

    Mirrors CLAUDE.md §2's QA command. Unused until the Dart SDK is installed;
    `StubAnalyzer` holds the seam open until then.
    """

    def __init__(self, executable: str = "dart") -> None:
        self.executable = executable

    def analyze(self, project_dir: Path) -> list[Diagnostic]:
        if shutil.which(self.executable) is None:
            raise ToolchainUnavailable(
                f"{self.executable!r} is not on PATH. Install the Flutter SDK, or run "
                f"the supervisor with --analyzer=stub."
            )
        proc = subprocess.run(
            [self.executable, "analyze", "--format=json", str(project_dir)],
            capture_output=True, text=True, check=False,
        )
        if not proc.stdout.strip():
            return []
        payload = json.loads(proc.stdout)
        return [
            Diagnostic(
                severity=d.get("severity", "error").lower(),
                file=d.get("location", {}).get("file", "<unknown>"),
                line=d.get("location", {}).get("range", {}).get("start", {}).get("line", 0),
                code=d.get("code", "dart"),
                message=d.get("problemMessage", ""),
            )
            for d in payload.get("diagnostics", [])
        ]


def get_analyzer(kind: str) -> DartAnalyzer:
    if kind == "stub":
        return StubAnalyzer()
    if kind == "dart":
        return DartSdkAnalyzer()
    raise ValueError(f"unknown analyzer {kind!r}; expected 'stub' or 'dart'")
