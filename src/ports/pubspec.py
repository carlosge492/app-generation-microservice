"""Declaring the dependencies the *harness* needs, rather than hoping for them.

The QA phase emits `integration_test/<model>_roundtrip_test.dart`,
`integration_test/navigation_test.dart` and `test_driver/integration_test.dart`
into every generated app. All three import `package:integration_test/...`, which
resolves only if the pubspec declares it as a dev dependency.

`TemplateGenerator`'s pubspec template happens to declare it. Nothing ever told
any other generator to, because the requirement does not come from the PRD — it
comes from files this repo writes after the generator has finished. The first
Claude-generated app therefore analysed with:

    error - Target of URI doesn't exist:
    'package:integration_test/integration_test_driver.dart'

which is the harness failing its own output. Worse than the error itself is
where it lands: those diagnostics are attributed to the generated app, so they
consume the repair budget and send a subagent off to fix code that was never
wrong.

A dependency the harness introduces is the harness's to declare.
"""

from __future__ import annotations

import re

# `integration_test` ships with the Flutter SDK, so it is `sdk: flutter` rather
# than a version constraint — a pinned version would fail to resolve.
_SDK_DEV_DEPENDENCIES = ("integration_test",)

_DEV_DEPENDENCIES = re.compile(r"^dev_dependencies:\s*$", re.M)
_TOP_LEVEL_KEY = re.compile(r"^[A-Za-z_][\w-]*:", re.M)


def _already_declared(pubspec: str, name: str) -> bool:
    """True if `name` is a dependency key anywhere in the file.

    Matched as an indented key rather than a bare substring: `integration_test`
    also appears in this project as a *directory* name, and a comment mentioning
    it is not a declaration.
    """
    return re.search(rf"^\s+{re.escape(name)}:\s*$", pubspec, re.M) is not None


def ensure_test_dependencies(pubspec: str) -> str:
    """Add the SDK dev dependencies the generated tests import, if missing.

    Text manipulation rather than a YAML round trip on purpose: parsing and
    re-emitting would strip every comment and reflow the generator's formatting,
    turning a two-line addition into a whole-file rewrite that no reviewer of
    the generated app could scan.
    """
    missing = [n for n in _SDK_DEV_DEPENDENCIES if not _already_declared(pubspec, n)]
    if not missing:
        return pubspec

    block = "".join(f"  {name}:\n    sdk: flutter\n" for name in missing)

    match = _DEV_DEPENDENCIES.search(pubspec)
    if match:
        # Insert at the top of the existing block, where it reads as part of the
        # list rather than as an afterthought appended past the last entry.
        at = match.end() + 1
        return pubspec[:at] + block + pubspec[at:]

    # No dev_dependencies at all. Put one before the `flutter:` section if there
    # is one, since trailing it after the asset/font declarations would read as
    # though it belonged to them.
    flutter_section = re.search(r"^flutter:\s*$", pubspec, re.M)
    insertion = f"dev_dependencies:\n{block}"
    if flutter_section:
        at = flutter_section.start()
        return pubspec[:at] + insertion + "\n" + pubspec[at:]

    separator = "" if pubspec.endswith("\n\n") else ("\n" if pubspec.endswith("\n") else "\n\n")
    return pubspec + separator + insertion
