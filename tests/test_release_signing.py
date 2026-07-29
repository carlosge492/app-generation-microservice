"""Release artifacts must not carry the debug key.

The failure this guards against is quiet. `flutter build apk --release` on a
stock scaffold succeeds, produces a plausible `app-release.apk`, and installs on
a device — and Play rejects it, because the template signs release builds with
the Android debug key. A buyer discovers that at upload, having already paid.

So the tests here are mostly about *not* believing things: that a Gradle patch
did what it intended, that an APK with `META-INF/` entries is signed, that a
missing tool means a clean artifact. Each of those guesses in the reassuring
direction, and each would ship a debug-signed APK labelled as a release build.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.build.pipeline import run_build
from src.build.signing import (
    SIGNED,
    UNKNOWN,
    UNSIGNED,
    Signature,
    find_apksigner,
    inspect_signature,
    unsign_release,
)

# The Flutter 3.44 scaffold, verbatim — including the TODO that is the bug.
KOTLIN_TEMPLATE = """plugins {
    id("com.android.application")
}

android {
    namespace = "com.example.fieldnotes"

    buildTypes {
        release {
            // TODO: Add your own signing config for the release build.
            // Signing with the debug keys for now, so `flutter run --release` works.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}
"""

GROOVY_TEMPLATE = """android {
    buildTypes {
        release {
            signingConfig signingConfigs.debug
        }
    }
}
"""


def _project(tmp_path: Path, filename: str, source: str) -> Path:
    app = tmp_path / "android" / "app"
    app.mkdir(parents=True)
    (app / filename).write_text(source, encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------- #
# Removing the debug signing config
# --------------------------------------------------------------------------- #


def test_the_debug_signing_config_is_removed_from_a_kotlin_scaffold(tmp_path):
    project = _project(tmp_path, "build.gradle.kts", KOTLIN_TEMPLATE)

    note = unsign_release(project)

    patched = (project / "android" / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    assert "signingConfigs" not in patched
    assert "removed 1" in note
    # The rest of the file has to survive, or the build stops working entirely.
    assert 'namespace = "com.example.fieldnotes"' in patched
    assert "buildTypes {" in patched


def test_the_older_groovy_dialect_is_handled_too(tmp_path):
    """`signingConfig signingConfigs.debug` — no `=`, no `getByName`. A pattern
    written only against the Kotlin DSL would silently miss this and leave the
    debug key wired in."""
    project = _project(tmp_path, "build.gradle", GROOVY_TEMPLATE)

    unsign_release(project)

    patched = (project / "android" / "app" / "build.gradle").read_text(encoding="utf-8")
    assert "signingConfigs" not in patched


def test_patching_twice_changes_nothing_further(tmp_path):
    project = _project(tmp_path, "build.gradle.kts", KOTLIN_TEMPLATE)

    unsign_release(project)
    once = (project / "android" / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    second_note = unsign_release(project)

    assert (project / "android" / "app" / "build.gradle.kts").read_text(encoding="utf-8") == once
    assert "no signingConfig assignment found" in second_note


def test_a_template_without_a_signing_config_is_reported_not_crashed(tmp_path):
    """A future Flutter might stop wiring the debug key. That is the outcome we
    want, so it is a note rather than an error — and the artifact check is what
    actually decides."""
    project = _project(tmp_path, "build.gradle.kts", "android {\n}\n")

    assert "no signingConfig assignment found" in unsign_release(project)


def test_a_project_with_no_android_directory_is_not_an_error(tmp_path):
    assert "no android/app/build.gradle" in unsign_release(tmp_path)


# --------------------------------------------------------------------------- #
# Reading a signature off the artifact
# --------------------------------------------------------------------------- #


def _apk(path: Path, entries: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def test_ordinary_meta_inf_entries_do_not_make_an_apk_signed(tmp_path, monkeypatch):
    """The trap in the fallback. A real release APK carries dozens of
    `META-INF/*` entries — library version markers, service descriptors — and
    matching the directory rather than the signature filenames would report
    every APK ever built as signed."""
    monkeypatch.setattr("src.build.signing.find_apksigner", lambda *_: None)
    apk = _apk(tmp_path / "app.apk", {
        "META-INF/androidx.core_core.version": "1.0",
        "META-INF/services/g3.d1": "x",
        "META-INF/version-control-info.textproto": "x",
        "classes.dex": "x",
    })

    signature = inspect_signature(apk)

    assert signature.state is not SIGNED
    # Without apksigner a v2-only signature is invisible, so this is `unknown`
    # rather than a clean bill of health.
    assert signature.state == UNKNOWN


def test_v1_signature_files_are_recognised_without_apksigner(tmp_path, monkeypatch):
    monkeypatch.setattr("src.build.signing.find_apksigner", lambda *_: None)
    apk = _apk(tmp_path / "app.apk", {
        "META-INF/MANIFEST.MF": "Manifest-Version: 1.0",
        "META-INF/CERT.SF": "Signature-Version: 1.0",
        "META-INF/CERT.RSA": "binary",
        "classes.dex": "x",
    })

    assert inspect_signature(apk).state == SIGNED


def test_a_missing_apksigner_yields_unknown_not_unsigned(tmp_path, monkeypatch):
    """The property the whole module turns on. `unknown` must never be treated
    as `unsigned`, or a deployment without build-tools silently starts claiming
    every artifact is clean."""
    monkeypatch.setattr("src.build.signing.find_apksigner", lambda *_: None)
    apk = _apk(tmp_path / "app.apk", {"classes.dex": "x"})

    signature = inspect_signature(apk)

    assert signature.state == UNKNOWN
    assert signature.is_unsigned is False


def test_unknown_is_not_unsigned():
    assert Signature(UNKNOWN, "no idea").is_unsigned is False
    assert Signature(SIGNED, "signed").is_unsigned is False
    assert Signature(UNSIGNED, "nothing signed it").is_unsigned is True


def test_a_missing_file_is_unknown(tmp_path):
    assert inspect_signature(tmp_path / "nope.apk").state == UNKNOWN


def test_apksigner_is_found_under_the_newest_build_tools(tmp_path):
    """Newest wins: an old build-tools left on a machine should not be what
    signs or verifies a release."""
    for version in ("34.0.0", "36.0.0", "35.0.0"):
        tools = tmp_path / "build-tools" / version
        tools.mkdir(parents=True)
        (tools / "apksigner.bat").write_text("", encoding="utf-8")

    assert "36.0.0" in find_apksigner(tmp_path)


# --------------------------------------------------------------------------- #
# The gate: what the pipeline does with all that
# --------------------------------------------------------------------------- #


def test_an_unknown_build_mode_is_refused(tmp_path):
    result = run_build(
        tmp_path, payment_verified=True, dry_run=False, prd=None, build_mode="profile"
    )
    # `prd=None` short-circuits first; the point is only that the mode is
    # validated rather than interpolated straight into a command line.
    assert result.status == "skipped" or "unknown build mode" in result.detail


def test_a_release_build_that_comes_out_signed_is_refused(tmp_path, monkeypatch):
    """The gate. If the Gradle patch silently failed, the artifact is signed
    with the debug key — handing it over is the harm this exists to prevent, so
    the build fails and no APK path is returned."""
    from src.build import pipeline

    project = _project(tmp_path, "build.gradle.kts", KOTLIN_TEMPLATE)
    apk_dir = project / "build" / "app" / "outputs" / "flutter-apk"
    apk_dir.mkdir(parents=True)
    _apk(apk_dir / "app-release.apk", {"classes.dex": "x"})

    monkeypatch.setattr(pipeline, "_flutter", lambda _root: "flutter")
    monkeypatch.setattr(pipeline, "ensure_android_scaffold", lambda *a, **k: False)
    monkeypatch.setattr(
        pipeline.subprocess, "run",
        lambda *a, **k: type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    monkeypatch.setattr(
        pipeline, "inspect_signature",
        lambda *a, **k: Signature(SIGNED, "signed with the debug key", "CN=Android Debug"),
    )

    result = run_build(
        project, payment_verified=True, prd=object(), dry_run=False, build_mode="release"
    )

    assert result.status == "failed"
    assert result.apk_path is None, "a signed release artifact must not be handed over"
    assert "CN=Android Debug" in result.detail


def test_a_release_build_whose_signature_cannot_be_read_is_also_refused(tmp_path, monkeypatch):
    """Same gate, the quieter case: no build-tools on the machine. Reporting an
    unverifiable artifact as a clean release is the failure mode."""
    from src.build import pipeline

    project = _project(tmp_path, "build.gradle.kts", KOTLIN_TEMPLATE)
    apk_dir = project / "build" / "app" / "outputs" / "flutter-apk"
    apk_dir.mkdir(parents=True)
    _apk(apk_dir / "app-release.apk", {"classes.dex": "x"})

    monkeypatch.setattr(pipeline, "_flutter", lambda _root: "flutter")
    monkeypatch.setattr(pipeline, "ensure_android_scaffold", lambda *a, **k: False)
    monkeypatch.setattr(
        pipeline.subprocess, "run",
        lambda *a, **k: type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    monkeypatch.setattr(
        pipeline, "inspect_signature", lambda *a, **k: Signature(UNKNOWN, "no apksigner")
    )

    result = run_build(
        project, payment_verified=True, prd=object(), dry_run=False, build_mode="release"
    )

    assert result.status == "failed"
    assert result.apk_path is None


def test_a_debug_build_is_not_subjected_to_the_signature_gate(tmp_path, monkeypatch):
    """Debug artifacts are signed with the debug key by definition. Applying the
    release gate to them would fail every debug build."""
    from src.build import pipeline

    project = _project(tmp_path, "build.gradle.kts", KOTLIN_TEMPLATE)
    apk_dir = project / "build" / "app" / "outputs" / "flutter-apk"
    apk_dir.mkdir(parents=True)
    _apk(apk_dir / "app-debug.apk", {"META-INF/CERT.RSA": "x"})

    monkeypatch.setattr(pipeline, "_flutter", lambda _root: "flutter")
    monkeypatch.setattr(pipeline, "ensure_android_scaffold", lambda *a, **k: False)
    monkeypatch.setattr(
        pipeline.subprocess, "run",
        lambda *a, **k: type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    result = run_build(
        project, payment_verified=True, prd=object(), dry_run=False, build_mode="debug"
    )

    assert result.status == "built"
    assert result.apk_path is not None


# --------------------------------------------------------------------------- #
# Picking the right artifact out of build/
# --------------------------------------------------------------------------- #


def test_a_stale_debug_apk_is_not_handed_over_as_the_release_build(tmp_path):
    """`build/` accumulates. `app-debug.apk` sorts ahead of
    `apk/release/app-release-unsigned.apk`, so taking the first match would give
    a buyer a debug artifact labelled as their release build."""
    from src.build.pipeline import _artifact

    outputs = tmp_path / "build" / "app" / "outputs"
    (outputs / "flutter-apk").mkdir(parents=True)
    (outputs / "apk" / "release").mkdir(parents=True)
    (outputs / "flutter-apk" / "app-debug.apk").write_bytes(b"")
    (outputs / "apk" / "release" / "app-release-unsigned.apk").write_bytes(b"")

    assert _artifact(tmp_path, "release").name == "app-release-unsigned.apk"
    assert _artifact(tmp_path, "debug").name == "app-debug.apk"


def test_a_build_directory_named_after_a_mode_does_not_confuse_the_match(tmp_path):
    """The absolute path is not what gets matched. A build directory called
    `release_check` would otherwise make every debug APK in it look like a
    release artifact — which is exactly what this repo's own verification
    directory is called."""
    from src.build.pipeline import _artifact

    project = tmp_path / "release_check"
    outputs = project / "build" / "app" / "outputs" / "flutter-apk"
    outputs.mkdir(parents=True)
    (outputs / "app-debug.apk").write_bytes(b"")

    assert _artifact(project, "release") is None
    assert _artifact(project, "debug") is not None


def test_packaging_is_still_gated_on_payment_in_release_mode(tmp_path):
    """The release path must not have introduced a way around the x402 gate."""
    from src.payments.x402 import PaymentNotVerified

    with pytest.raises(PaymentNotVerified):
        run_build(tmp_path, payment_verified=False, dry_run=False, build_mode="release")
