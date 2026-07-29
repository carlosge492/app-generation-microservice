"""Packaging step — turns the analysed project into an installable APK.

Gated on x402: `require_verified` is the first statement, so no code path
reaches a build without a verified payment.

This calls `flutter build apk` rather than the `fastlane android build_m2m_apk`
lane CLAUDE.md originally specified. fastlane is a Ruby tool and every lane it
would run here is a wrapper around this command; dropping it removes a whole
language runtime from the build environment and from CI. Reinstate it when there
are signing or Play-upload steps that actually justify it.

`build_mode="release"` additionally strips the debug signing config the Flutter
template wires into release builds, and then refuses to return an artifact it
cannot show to be unsigned. The reasoning for emitting unsigned releases at all
is in `signing.py`; the short version is that the service must not hold buyers'
release keys.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.build.scaffold import ensure_android_scaffold
from src.build.signing import inspect_signature, unsign_release
from src.payments.x402 import require_verified
from src.prd.schema import PRD


BUILD_MODES = ("debug", "release")


@dataclass(frozen=True)
class BuildResult:
    status: str  # "built" | "skipped" | "failed"
    detail: str
    apk_path: str | None = None
    # Only meaningful for release builds, where "who signed this?" is the
    # difference between a shippable artifact and one Play will reject.
    signature: str | None = None


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


def _artifact(project_dir: Path, build_mode: str) -> Path | None:
    """The APK this build produced — not merely the first one lying around.

    `build/` accumulates: a debug run leaves `app-debug.apk` behind, and it sorts
    ahead of `apk/release/app-release-unsigned.apk`. Taking the first match would
    hand a buyer a debug artifact labelled as their release build. The signature
    gate would catch that one, having been signed with the debug key, but as a
    baffling failure rather than the bug it is.

    Gradle also names the output `-unsigned` when no signing config exists at
    all, and puts it under `apk/release/` rather than the `flutter-apk/` path
    Flutter copies signed builds to — so matching on mode rather than filename
    is what keeps this working in both cases.

    Matched against the path *below* the project, never the absolute one: a
    build directory that happens to be called `release_check` would otherwise
    make every debug artifact in it look like a release build.
    """
    build_root = project_dir / "build"
    for apk in sorted(build_root.rglob("*.apk")):
        if build_mode in apk.relative_to(build_root).as_posix().lower():
            return apk
    return None


def run_build(
    project_dir: Path,
    *,
    payment_verified: bool,
    prd: PRD | None = None,
    flutter_root: str | Path | None = None,
    dry_run: bool = False,
    build_mode: str = "debug",
    sdk_root: str | Path | None = None,
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

    if build_mode not in BUILD_MODES:
        return BuildResult("failed", f"unknown build mode {build_mode!r}; expected one of {BUILD_MODES}")

    try:
        created = ensure_android_scaffold(project_dir, prd, flutter)
    except RuntimeError as exc:
        return BuildResult("failed", str(exc))

    notes: list[str] = []
    if build_mode == "release":
        # Before the build, or Gradle signs with the debug key and the artifact
        # has to be thrown away rather than corrected.
        notes.append(unsign_release(project_dir))

    proc = subprocess.run(
        [flutter, "build", "apk", f"--{build_mode}"],
        cwd=project_dir, capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace",  # see analyzer.py
    )
    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-800:]
        return BuildResult("failed", f"`flutter build apk` exited {proc.returncode}: {tail}")

    apk = _artifact(project_dir, build_mode)
    if apk is None:
        return BuildResult("failed", "flutter reported success but no .apk was produced")

    size_mb = apk.stat().st_size / 1_048_576
    scaffold_note = "scaffolded android/, " if created else ""
    summary = f"{scaffold_note}{build_mode} APK {size_mb:.1f} MB"

    if build_mode != "release":
        return BuildResult("built", summary, str(apk))

    # The artifact decides, not the patch. A release APK that is signed — or
    # that cannot be shown to be unsigned — is worse than no artifact: the buyer
    # would discover it at upload, having already paid.
    signature = inspect_signature(apk, sdk_root)
    if not signature.is_unsigned:
        signer = f" (signer: {signature.signer})" if signature.signer else ""
        return BuildResult(
            "failed",
            f"release APK is not verifiably unsigned — {signature.state}: "
            f"{signature.detail}{signer}. Refusing to hand over a release build "
            "that may carry the debug key.",
            None,
            signature.state,
        )

    return BuildResult(
        "built",
        f"{summary}, unsigned ({'; '.join(n for n in notes if n)})",
        str(apk),
        signature.state,
    )
