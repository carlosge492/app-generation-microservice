"""Reclaiming the ~2.0 GB a finished build leaves on disk.

The shapes here are taken from the first real deployment rather than invented:

    build_dir  /data/builds/476ebedc179241f9a2a88e72ed8246e3
    apk_path   .../build/app/outputs/apk/debug/app-debug.apk

which is the detail that matters — the artifact is produced several directories
deep inside the tree that has to be deleted, so anything that removes the tree
without relocating the APK first destroys what the buyer paid for.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from src.service.artifacts import keep_only_the_artifact, sweep_expired


def make_build(root: Path, job_id: str, *, with_apk: bool = True) -> tuple[Path, str | None]:
    """A build directory shaped like one the pipeline produces."""
    build_dir = root / job_id
    (build_dir / "lib" / "ui").mkdir(parents=True)
    (build_dir / "lib" / "ui" / "home_screen.dart").write_text("// widget tree\n")
    (build_dir / "android" / "app").mkdir(parents=True)
    (build_dir / "android" / "app" / "build.gradle.kts").write_text("plugins {}\n")
    (build_dir / ".dart_tool").mkdir()
    (build_dir / ".dart_tool" / "package_config.json").write_text("{}\n")
    (build_dir / "pubspec.yaml").write_text("name: demo\n")

    apk = None
    if with_apk:
        outputs = build_dir / "build" / "app" / "outputs" / "apk" / "debug"
        outputs.mkdir(parents=True)
        apk = outputs / "app-debug.apk"
        apk.write_bytes(b"PK\x03\x04" + b"x" * 4096)
    return build_dir, (str(apk) if apk else None)


def test_the_apk_survives_and_everything_else_goes(tmp_path):
    build_dir, apk_path = make_build(tmp_path, "job1")

    kept = keep_only_the_artifact(build_dir, apk_path)

    assert kept is not None
    assert Path(kept).is_file()
    assert Path(kept).read_bytes().startswith(b"PK")
    assert Path(kept).parent == build_dir, "the artifact moves to the top of the job dir"
    assert not (build_dir / "lib").exists()
    assert not (build_dir / "build").exists()
    assert not (build_dir / ".dart_tool").exists()
    assert not (build_dir / "pubspec.yaml").exists()


def test_the_returned_path_is_where_the_apk_now_is(tmp_path):
    """The job record points at apk_path, and `apk_available` stats it. A caller
    that ignored the return value would leave the record naming a file that was
    just deleted — the buyer would see the build succeed and the download 404."""
    build_dir, apk_path = make_build(tmp_path, "job2")

    kept = keep_only_the_artifact(build_dir, apk_path)

    assert kept != apk_path, "it did move"
    assert not Path(apk_path).exists()
    assert Path(kept).exists()


def test_a_failed_build_leaves_nothing_behind(tmp_path):
    """No artifact means nothing worth keeping: the log and diagnostics are on
    the job record, and a retry rebuilds from the stored PRD."""
    build_dir, _ = make_build(tmp_path, "job3", with_apk=False)

    kept = keep_only_the_artifact(build_dir, None)

    assert kept is None
    assert build_dir.is_dir()
    assert list(build_dir.iterdir()) == []


def test_an_artifact_outside_the_build_dir_is_not_moved(tmp_path):
    """Relocating a path that is not ours would be a surprise found much later."""
    build_dir, _ = make_build(tmp_path, "job4", with_apk=False)
    elsewhere = tmp_path / "somewhere-else.apk"
    elsewhere.write_bytes(b"PK\x03\x04")

    kept = keep_only_the_artifact(build_dir, str(elsewhere))

    assert kept == str(elsewhere)
    assert elsewhere.exists()


def test_pruning_a_missing_directory_is_not_an_error(tmp_path):
    """A build that never got as far as creating its directory still finishes."""
    assert keep_only_the_artifact(tmp_path / "never-created", None) is None


def test_the_sweep_removes_old_jobs_and_keeps_recent_ones(tmp_path):
    old, _ = make_build(tmp_path, "old-job")
    recent, _ = make_build(tmp_path, "recent-job")
    stale = time.time() - (8 * 24 * 3600)
    os.utime(old, (stale, stale))

    freed = sweep_expired(tmp_path, retention_seconds=7 * 24 * 3600)

    assert not old.exists()
    assert recent.exists()
    assert freed > 0


def test_the_sweep_leaves_loose_files_alone(tmp_path):
    """Only job directories are the service's to delete. Anything else under
    BUILD_ROOT was put there by somebody else."""
    note = tmp_path / "README"
    note.write_text("mounted volume\n")
    stale = time.time() - (30 * 24 * 3600)
    os.utime(note, (stale, stale))

    sweep_expired(tmp_path, retention_seconds=7 * 24 * 3600)

    assert note.exists()
