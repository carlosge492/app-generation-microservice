"""Picking the right SDK executable for the platform being run on.

This is a regression test for a bug that a full verification suite, an eval
sweep and an APK packaging run all failed to catch, because every one of them
ran on Windows. The Flutter SDK ships `flutter` *and* `flutter.bat` on every
platform, and four call sites took the first that `exists()`, Windows spelling
first. On Linux that resolves to the batch file:

    PermissionError: [Errno 13] Permission denied: '/opt/flutter/bin/flutter.bat'

which is what the first deployed build did, after the payment had settled
on-chain.

Both platform branches are therefore exercised here regardless of the host —
`windows=` is an explicit parameter rather than a module constant for exactly
that reason. A test that could only check the branch it happens to be running on
would have passed against the broken code.
"""

from __future__ import annotations

import os
import sys

import pytest

from src.ports.toolchain import sdk_executable


@pytest.fixture
def sdk(tmp_path):
    """A `bin/` holding both spellings, as a real Flutter SDK does."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "flutter").write_text("#!/bin/sh\n")
    (bin_dir / "flutter.bat").write_text("@echo off\n")
    if os.name != "nt":
        os.chmod(bin_dir / "flutter", 0o755)
        os.chmod(bin_dir / "flutter.bat", 0o644)
    return bin_dir


def test_posix_never_picks_the_batch_file(sdk):
    """The bug, in one assertion. `.bat` is present on Linux and is not a
    program; picking it fails at exec time, not at resolution time."""
    resolved = sdk_executable(sdk, "flutter", windows=False)

    assert resolved is not None
    assert resolved.endswith("flutter")
    assert not resolved.endswith(".bat")


def test_windows_picks_the_batch_file(sdk):
    """The extensionless file on Windows is a shell script that cannot run, so
    the fix must not simply invert the old order."""
    resolved = sdk_executable(sdk, "flutter", windows=True)

    assert resolved is not None
    assert resolved.endswith("flutter.bat")


@pytest.mark.skipif(os.name == "nt", reason="Windows has no executable bit")
def test_posix_ignores_a_file_it_cannot_execute(tmp_path):
    """`exists()` was the wrong question. An SDK unpacked without the executable
    bit should report "not found" rather than hand back an unrunnable path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "flutter").write_text("#!/bin/sh\n")
    os.chmod(bin_dir / "flutter", 0o644)

    assert sdk_executable(bin_dir, "flutter", windows=False) is None


def test_missing_tool_is_none_not_a_guess(tmp_path):
    assert sdk_executable(tmp_path, "flutter", windows=False) is None
    assert sdk_executable(tmp_path, "flutter", windows=True) is None


def test_the_default_follows_the_host(sdk):
    """Callers that pass nothing must get the host's answer, since that is what
    every production call site does."""
    resolved = sdk_executable(sdk, "flutter")

    expected = ".bat" if sys.platform == "win32" else "flutter"
    assert resolved.endswith(expected)


def test_every_resolver_goes_through_this_helper():
    """Four call sites had the same bug independently. The point of the helper
    is that there is now one place to be wrong, so a new `.bat`-first loop
    appearing anywhere in src/ is a regression."""
    import pathlib
    import re

    offenders = []
    for path in pathlib.Path("src").rglob("*.py"):
        if path.name == "toolchain.py":
            continue
        body = path.read_text(encoding="utf-8")
        if re.search(r'["\']\w+\.bat["\']', body):
            offenders.append(str(path))

    assert not offenders, f"resolve executables via sdk_executable(): {offenders}"
