"""Android platform scaffolding.

The loop generates `lib/`, `pubspec.yaml` and `test/` — everything needed to
analyse and unit-test. Producing an APK additionally needs the Gradle project
under `android/`, which only `flutter create` can lay down.

It is generated into a temporary directory and only `android/` is copied across,
rather than running `flutter create` over the build directory. `flutter create`
writes its own `lib/main.dart` and `test/widget_test.dart` referencing a counter
app; run in place it would either clobber generated code or leave a stale test
that fails the analysis gate.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from src.prd.schema import PRD


def _split_application_id(package_name: str) -> tuple[str, str]:
    """`com.example.fieldnotes` -> (`com.example`, `fieldnotes`).

    `flutter create` builds the applicationId as <org>.<project-name>, so
    splitting this way reproduces the PRD's package_name exactly rather than
    letting it drift to <org>.<dart_package_name>.
    """
    org, _, name = package_name.rpartition(".")
    return org, name


def ensure_android_scaffold(
    project_dir: Path, prd: PRD, flutter: str, *, force: bool = False
) -> bool:
    """Add `android/` to the build directory. Returns True if it was created."""
    target = project_dir / "android"
    if target.exists() and not force:
        return False

    org, name = _split_application_id(prd.package_name)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "scaffold"
        proc = subprocess.run(
            [
                flutter, "create",
                "--platforms=android",
                "--org", org,
                "--project-name", name,
                str(staging),
            ],
            capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace",  # see analyzer.py
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"`flutter create` failed ({proc.returncode}):\n"
                f"{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"
            )
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(staging / "android", target)
    return True
