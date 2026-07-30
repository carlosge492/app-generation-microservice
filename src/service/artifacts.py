"""Reclaiming the disk a finished build leaves behind.

A completed build is about 2.0 GB — a Flutter project, its `.dart_tool`, an
`android/` scaffold and a Gradle output tree — of which the buyer wants roughly
150 MB: the APK. On the first deployment that meant a 150 GB box had room for
about 58 sales.

Nothing reclaimed it. Job *records* expire from Redis after seven days, which is
the wrong half: the record is a few kilobytes and the directory it points at is
gigabytes. Left alone the disk fills, and the failure lands on a build that has
already been paid for, which is the worst failure this service has.

Two mechanisms, deliberately separate:

* `keep_only_the_artifact` runs the moment a build finishes. The APK is moved to
  the top of the job's directory and everything else goes. This is where the
  2.0 GB actually is.
* `sweep_expired` removes whole job directories past their retention. It matches
  `RedisJobStore`'s TTL, because once the record is gone the download endpoint
  answers 404 and the APK on disk is unreachable weight.

Both are conservative about what they will delete: only inside `BUILD_ROOT`, only
directories that look like the job directories this service created, and never
the artifact a buyer has paid for and might still be collecting.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_RETENTION_SECONDS = 7 * 24 * 3600


def _tree_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def keep_only_the_artifact(build_dir: str | Path, apk_path: str | None) -> str | None:
    """Delete the project tree, keeping the APK. Returns its new path.

    The APK is produced deep inside the build — Gradle puts it at
    `build/app/outputs/apk/<mode>/app-<mode>.apk` — so it has to be moved out
    before the tree can go. The returned path is what the job record should
    point at; callers that ignore the return value would leave the record
    referring to a file that has just been deleted.

    A build with no artifact (a failure) has its tree removed too: the log and
    diagnostics live on the job record, which is what anyone debugging reads,
    and a failed build is retried from the stored PRD rather than from whatever
    is left on disk.
    """
    build_dir = Path(build_dir)
    if not build_dir.is_dir():
        return apk_path

    freed_from = _tree_size(build_dir)
    kept: Path | None = None

    if apk_path:
        source = Path(apk_path)
        # Only relocate an artifact that is actually inside this job's tree.
        # An apk_path pointing elsewhere is not ours to move, and moving it
        # would be the kind of surprise that is discovered much later.
        if source.is_file() and source.resolve().is_relative_to(build_dir.resolve()):
            kept = build_dir / source.name
            if kept.resolve() != source.resolve():
                # Delete-then-replace rather than overwrite: a stale artifact of
                # the same name from an earlier attempt must not survive.
                if kept.exists():
                    kept.unlink()
                shutil.move(str(source), str(kept))
        elif source.is_file():
            kept = source

    for entry in build_dir.iterdir():
        if kept is not None and entry.resolve() == kept.resolve():
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove %s while pruning %s", entry, build_dir)

    freed = freed_from - _tree_size(build_dir)
    log.info(
        "pruned %s, freeing %.1f GB; kept %s",
        build_dir, freed / 1e9, kept.name if kept else "nothing",
    )
    return str(kept) if kept else None


def sweep_expired(
    build_root: str | Path, retention_seconds: int = DEFAULT_RETENTION_SECONDS
) -> int:
    """Remove job directories older than the retention. Returns bytes freed.

    Age is taken from the directory's own mtime, which the build updates as it
    writes — so a job that was retried days after it was created is measured
    from the retry, not from the payment.
    """
    build_root = Path(build_root)
    if not build_root.is_dir():
        return 0

    cutoff = time.time() - retention_seconds
    freed = 0
    for entry in build_root.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            size = _tree_size(entry)
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                freed += size
                log.info("swept expired build %s (%.2f GB)", entry.name, size / 1e9)
        except OSError:
            log.warning("could not sweep %s", entry)
    return freed
