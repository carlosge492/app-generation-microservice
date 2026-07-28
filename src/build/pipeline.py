"""Packaging step — turns the analysed project into an installable APK.

Gated on x402: `require_verified` is the first statement, so no code path
reaches a build without a verified payment.

This calls `flutter build apk` rather than the `fastlane android build_m2m_apk`
lane CLAUDE.md originally specified. fastlane is a Ruby tool and every lane it
would run here is a wrapper around this command; dropping it removes a whole
language runtime from the build environment and from CI. Reinstate it when there
are signing or Play-upload steps that actually justify it.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.build.scaffold import ensure_android_scaffold
from src.payments.x402 import require_verified
from src.prd.schema import PRD


@dataclass(frozen=True)
class BuildResult:
    status: str  # "built" | "skipped" | "failed"
    detail: str
    apk_path: str | None = None


def _flutter(flutter_root: str | Path | None) -> str:
    if flutter_root:
        for candidate in ("flutter.bat", "flutter"):
            path = Path(flutter_root) / "bin" / candidate
            if path.exists():
                return str(path)
    found = shutil.which("flutter")
    if found is None:
        raise RuntimeError(
            "flutter is not on PATH and no --flutter-root was given; cannot package"
        )
    return found


def run_build(
    project_dir: Path,
    *,
    payment_verified: bool,
    prd: PRD | None = None,
    flutter_root: str | Path | None = None,
    dry_run: bool = False,
    build_mode: str = "debug",
) -> BuildResult:
    # Payment gate first: nothing below is reachable without it.
    require_verified(payment_verified)

    if dry_run:
        return BuildResult("skipped", "dry-run: no APK built")
    if prd is None:
        return BuildResult("skipped", "no PRD supplied; cannot scaffold the Android project")

    try:
        flutter = _flutter(flutter_root)
    except RuntimeError as exc:
        return BuildResult("skipped", str(exc))

    try:
        created = ensure_android_scaffold(project_dir, prd, flutter)
    except RuntimeError as exc:
        return BuildResult("failed", str(exc))

    proc = subprocess.run(
        [flutter, "build", "apk", f"--{build_mode}"],
        cwd=project_dir, capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace",  # see analyzer.py
    )
    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-800:]
        return BuildResult("failed", f"`flutter build apk` exited {proc.returncode}: {tail}")

    apk = next(
        iter(sorted((project_dir / "build").rglob("*.apk"))), None
    )
    if apk is None:
        return BuildResult("failed", "flutter reported success but no .apk was produced")

    size_mb = apk.stat().st_size / 1_048_576
    scaffold_note = "scaffolded android/, " if created else ""
    return BuildResult(
        "built",
        f"{scaffold_note}{build_mode} APK {size_mb:.1f} MB",
        str(apk),
    )
