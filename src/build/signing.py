"""Release signing — what this service does, and deliberately what it does not.

`flutter create` scaffolds a release build type that signs with the **debug**
key:

    buildTypes {
        release {
            // TODO: Add your own signing config for the release build.
            // Signing with the debug keys for now, so `flutter run --release` works.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

That TODO is the whole problem. Left alone, `flutter build apk --release`
succeeds, produces a plausible `app-release.apk`, installs on a device, and is
not shippable: the Android debug key is a well-known development credential, and
Play rejects anything signed with it. A buyer paying for a build would receive
an artifact that looks finished and cannot be published — a failure that only
surfaces at upload, long after the money moved.

**The service does not sign.** A release key is the credential that decides who
can ship updates to an app's installed base; holding buyers' keys would mean
holding that power over every app this service ever generated, and one breach
would compromise all of them at once. So the release artifact is emitted
*unsigned*, and the buyer signs it with a key the service never sees. This is
less impressive than handing over an installable APK and strictly more correct.

Two things make that safe rather than merely stated:

**The Gradle edit is best-effort; the artifact is the gate.** Patching generated
Gradle by pattern-matching a template that changes between Flutter versions is
not something to stake a security property on. So `unsign_release` does its best
and reports what it did, and `inspect_signature` then examines the APK that
actually came out. The build fails if a release artifact turns out to be signed,
whatever the patch believed.

**Unknown is not clean.** If the signature state cannot be established — no
`apksigner`, an unreadable file — the answer is `unknown`, and the caller treats
that as "do not claim this is unsigned". Guessing in the reassuring direction is
how a debug-signed APK reaches a buyer labelled as a release build.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Both dialects: Kotlin DSL (`signingConfig = signingConfigs.getByName("debug")`)
# and the older Groovy one (`signingConfig signingConfigs.debug`).
_SIGNING_CONFIG_LINE = re.compile(
    r"^[ \t]*signingConfig[ \t]*(?:=[ \t]*)?signingConfigs[ \t]*[.(].*$",
    re.MULTILINE,
)

_REPLACEMENT = (
    "            // Intentionally unsigned. The generating service never holds a\n"
    "            // buyer's release key; sign this artifact yourself. The stock\n"
    "            // Flutter template signs release with the debug key here, which\n"
    "            // builds green and cannot be published."
)

# Signature files as they appear inside the archive. v1 (JAR) signing only —
# v2/v3 live in the APK Signing Block, which is not a zip entry, so this list is
# a fallback and never the authority. Note that a release APK legitimately
# carries many other `META-INF/*` entries (library version markers, service
# descriptors); matching on the directory alone would call every APK signed.
_V1_SIGNATURE_ENTRIES = re.compile(
    r"^META-INF/(?:MANIFEST\.MF|[^/]+\.(?:RSA|DSA|EC|SF))$", re.IGNORECASE
)

SIGNED = "signed"
UNSIGNED = "unsigned"
UNKNOWN = "unknown"


class ReleaseSigningError(RuntimeError):
    """A release artifact could not be shown to be unsigned."""


@dataclass(frozen=True)
class Signature:
    """What an APK's signature state is, and how confidently we know it."""

    state: str  # SIGNED | UNSIGNED | UNKNOWN
    detail: str
    signer: str | None = None

    @property
    def is_unsigned(self) -> bool:
        """True only for a positive finding. `unknown` is not `unsigned`."""
        return self.state == UNSIGNED


def unsign_release(project_dir: Path) -> str:
    """Strip the debug signing config from the generated release build type.

    Returns a human-readable note on what happened. Finding nothing to patch is
    not an error: a template that never wired a signing config produces an
    unsigned release already, and `inspect_signature` is what decides whether
    the outcome was actually achieved.
    """
    gradle = _app_gradle(project_dir)
    if gradle is None:
        return "no android/app/build.gradle(.kts) to patch"

    source = gradle.read_text(encoding="utf-8")
    patched, count = _SIGNING_CONFIG_LINE.subn(_REPLACEMENT, source)
    if count == 0:
        return f"{gradle.name}: no signingConfig assignment found; left as generated"

    gradle.write_text(patched, encoding="utf-8")
    return f"{gradle.name}: removed {count} signingConfig assignment(s) from the release build"


def _app_gradle(project_dir: Path) -> Path | None:
    for name in ("build.gradle.kts", "build.gradle"):
        candidate = project_dir / "android" / "app" / name
        if candidate.exists():
            return candidate
    return None


def find_apksigner(sdk_root: str | Path | None = None) -> str | None:
    """Locate `apksigner`, preferring the newest build-tools available.

    Checked in the order an operator would expect to win: an explicit argument,
    then the standard Android SDK environment variables, then PATH.
    """
    roots = [
        sdk_root,
        os.getenv("ANDROID_SDK_ROOT"),
        os.getenv("ANDROID_HOME"),
    ]
    for root in roots:
        if not root:
            continue
        build_tools = Path(root) / "build-tools"
        if not build_tools.is_dir():
            continue
        for version in sorted(build_tools.iterdir(), reverse=True):
            for name in ("apksigner.bat", "apksigner"):
                candidate = version / name
                if candidate.exists():
                    return str(candidate)
    return shutil.which("apksigner")


def inspect_signature(apk: Path, sdk_root: str | Path | None = None) -> Signature:
    """Report whether `apk` carries a signature.

    `apksigner` is the authority because it reads the APK Signing Block, where
    v2 and v3 signatures live; those are invisible to a zip listing, so the
    fallback below can only ever prove the v1 case. An APK the fallback calls
    unsigned may still carry a v2 signature, which is why a missing `apksigner`
    yields `unknown` rather than `unsigned`.
    """
    if not apk.exists():
        return Signature(UNKNOWN, f"no such file: {apk}")

    apksigner = find_apksigner(sdk_root)
    if apksigner is not None:
        return _inspect_with_apksigner(apksigner, apk)

    found = _v1_signature_entries(apk)
    if found:
        return Signature(SIGNED, f"v1 signature files present: {', '.join(found)}")
    return Signature(
        UNKNOWN,
        "apksigner was not found, and a zip listing cannot see v2/v3 signatures; "
        "install Android build-tools or set ANDROID_SDK_ROOT to decide this",
    )


def _inspect_with_apksigner(apksigner: str, apk: Path) -> Signature:
    proc = subprocess.run(
        [apksigner, "verify", "--print-certs", str(apk)],
        capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace",  # see analyzer.py
    )
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode == 0:
        return Signature(SIGNED, "apksigner verified a signature", _signer(output))

    # A missing manifest is apksigner's way of saying "nothing signed this".
    # Any other non-zero exit is a broken or unreadable APK, which is not the
    # same finding and must not be reported as a clean unsigned artifact.
    if "Missing META-INF/MANIFEST.MF" in output or "DOES NOT VERIFY" in output:
        if _v1_signature_entries(apk):
            return Signature(
                SIGNED, f"signature files present but apksigner rejected them: {output[:200]}"
            )
        return Signature(UNSIGNED, "apksigner reports no signature")
    return Signature(UNKNOWN, f"apksigner exited {proc.returncode}: {output[:200]}")


def _signer(output: str) -> str | None:
    for line in output.splitlines():
        if "certificate DN:" in line:
            return line.split("certificate DN:", 1)[1].strip()
    return None


def _v1_signature_entries(apk: Path) -> list[str]:
    try:
        with zipfile.ZipFile(apk) as archive:
            return [n for n in archive.namelist() if _V1_SIGNATURE_ENTRIES.match(n)]
    except (zipfile.BadZipFile, OSError):
        log.exception("could not read %s as a zip", apk)
        return []
