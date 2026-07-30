"""Run the generated widget tests as a second QA gate.

`flutter analyze` proves the code compiles; this proves it runs. The two catch
disjoint failure classes — a Material widget inside a CupertinoApp analyses
perfectly and throws on first build.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from src.ports.analyzer import Diagnostic, ToolchainUnavailable
from src.ports.toolchain import sdk_executable

# `  D:/path/to/test/foo_test.dart: SomeScreen builds without throwing`
_FAILING = re.compile(r"^\s*(\S+?\.dart):\s*(.+?)\s*$")


class TestRunner(Protocol):
    def run(self, project_dir: Path) -> list[Diagnostic]: ...


class FlutterTestRunner:
    def __init__(self, flutter_root: str | Path | None = None) -> None:
        self.flutter_root = Path(flutter_root) if flutter_root else None

    def _tool(self) -> str:
        if self.flutter_root:
            tool = sdk_executable(self.flutter_root / "bin", "flutter")
            if tool is not None:
                return tool
            raise ToolchainUnavailable(f"flutter not found under {self.flutter_root / 'bin'}")
        found = shutil.which("flutter")
        if found is None:
            raise ToolchainUnavailable(
                "flutter is not on PATH and no --flutter-root was given"
            )
        return found

    def run(self, project_dir: Path) -> list[Diagnostic]:
        if not (project_dir / "test").exists():
            return []

        proc = subprocess.run(
            [self._tool(), "test", "--reporter", "compact"],
            cwd=project_dir, capture_output=True, text=True, check=False,
            # Flutter emits UTF-8; Windows would otherwise decode with cp1252,
            # and a single undecodable byte kills subprocess's reader thread,
            # leaving stdout as None. That surfaced as
            # "TypeError: unsupported operand type(s) for +: 'NoneType' and
            # 'str'" five PRDs into a sweep, nowhere near the real cause.
            encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0:
            return []
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return _parse_runner_output(combined, project_dir)


def _parse_runner_output(output: str, project_dir: Path) -> list[Diagnostic]:
    """Split out from `run` so the parsing is testable with no SDK present."""
    _, _, tail = output.partition("Failing tests:")
    out: list[Diagnostic] = []

    for line in tail.splitlines():
        match = _FAILING.match(line)
        if not match:
            continue
        raw_path, test_name = match.groups()
        try:
            rel = Path(raw_path).resolve().relative_to(project_dir.resolve()).as_posix()
        except (ValueError, OSError):
            rel = raw_path.replace("\\", "/")
        out.append(Diagnostic(
            "error", rel, 0, "smoke_failure",
            f"widget test failed: {test_name} — the screen compiles but does not "
            f"build at runtime",
        ))

    if not out:
        # Tests failed but the summary was not parseable. Never report green on
        # a non-zero exit: a silently-swallowed failure is worse than a noisy one.
        out.append(Diagnostic(
            "error", "test/", 0, "smoke_failure",
            f"`flutter test` failed: {output.strip()[-400:]}",
        ))
    return out
