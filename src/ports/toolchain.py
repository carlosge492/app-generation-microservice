"""Finding an SDK's executables, on the platform actually being run on.

The Flutter SDK ships **both** `flutter` and `flutter.bat` in `bin/`, on every
platform — the batch file is not omitted from the Linux tarball. Four call sites
independently resolved the tool by taking the first candidate that *existed*,
with the Windows spelling first, which is correct on Windows and picks an
unrunnable batch file everywhere else:

    PermissionError: [Errno 13] Permission denied: '/opt/flutter/bin/flutter.bat'

That is what the first Linux deployment did, after settling a payment on-chain.
Every check the project had ran on Windows, where the bug is invisible, so it
survived a full verification suite and an eval sweep and was found by a buyer
paying for a build. The lesson is not "test on Linux" but that `exists()` is the
wrong question for an executable; `windows=` is a parameter here rather than a
module constant precisely so the other platform's branch is reachable from a
test on either.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def sdk_executable(
    directory: str | Path, name: str, *, windows: bool | None = None
) -> str | None:
    """The runnable `name` inside `directory`, or None if there is not one.

    On Windows the launcher is `name.bat` (or `.exe`) and the extensionless file
    is a shell script that cannot run. On POSIX the reverse holds, and a `.bat`
    is never a candidate — it exists in the SDK, it is simply not a program.
    """
    if windows is None:
        windows = sys.platform == "win32"

    directory = Path(directory)
    candidates = (f"{name}.bat", f"{name}.exe", name) if windows else (name,)

    for candidate in candidates:
        path = directory / candidate
        if not path.is_file():
            continue
        # The executable bit is the whole point on POSIX. Windows does not carry
        # one, and os.access(X_OK) there answers about extensions rather than
        # permissions, so asking would only introduce a different wrong answer.
        if windows or os.access(path, os.X_OK):
            return str(path)
    return None
