"""Packaging step — the `fastlane android build_m2m_apk` wrapper from CLAUDE.md §2.

Gated on x402 and on fastlane actually being installed. Neither is true in the
current environment, so `run_build` reports `skipped` rather than pretending.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.payments.x402 import require_verified

FASTLANE_LANE = "android build_m2m_apk"


@dataclass(frozen=True)
class BuildResult:
    status: str  # "built" | "skipped"
    detail: str
    apk_path: str | None = None


def run_build(project_dir: Path, *, payment_verified: bool, dry_run: bool = False) -> BuildResult:
    # Payment gate first: never reachable without it.
    require_verified(payment_verified)

    if dry_run:
        return BuildResult("skipped", "dry-run: fastlane not invoked")

    if shutil.which("fastlane") is None:
        return BuildResult(
            "skipped",
            "fastlane is not installed; payment gate passed but the toolchain is absent",
        )

    proc = subprocess.run(
        ["fastlane", *FASTLANE_LANE.split()],
        cwd=project_dir, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return BuildResult("skipped", f"fastlane exited {proc.returncode}: {proc.stderr[-500:]}")

    apk = next(iter(sorted(project_dir.rglob("*.apk"))), None)
    return BuildResult("built", "fastlane completed", str(apk) if apk else None)
