"""Which subagent can actually fix a given diagnostic.

CLAUDE.md §4 says to feed a failing stack trace back to the Logic subagent. That
is right for the desync case it describes, but it is not a universal rule: a
diagnostic anchored in `lib/ui/` is unfixable by an agent that may only write
`lib/providers/`. Routing every diagnostic to Logic burns the repair budget and
fails the build.

So: explicit per-code ownership where we know better than the file path, and a
path-based default for the long tail of real `dart analyze` codes.
"""

from __future__ import annotations

from typing import Literal

from src.ports.analyzer import Diagnostic

Owner = Literal["genui", "logic", "fatal"]

# Codes whose owner is not implied by the file they are anchored in.
_EXPLICIT: dict[str, Owner] = {
    # Anchored in lib/ui/, but the UI is correct and the Logic subagent is the
    # one that must declare the provider. This is CLAUDE.md §4's desync case.
    "undefined_provider": "logic",
    # Anchored in lib/ui/ and genuinely the UI's fault.
    "setstate_in_ui": "genui",
    "logic_in_ui": "genui",
    # Anchored in lib/providers/ and genuinely the Logic agent's fault.
    "ui_in_logic": "logic",
    # Conformance: the app compiles but is not the app that was specified.
    "theme_mismatch": "genui",
    "missing_screen": "genui",
    "unreachable_screen": "genui",
    "missing_form_field": "genui",
    "missing_provider": "logic",
    "wrong_collection": "logic",
    # Planning-phase output is frozen once GenUI starts; nothing downstream can
    # repair it. Changes flow downstream, never upstream.
    "missing_pubspec": "fatal",
    "incomplete_pubspec": "fatal",
    "unparseable_pubspec": "fatal",
}


def owner_of(diagnostic: Diagnostic) -> Owner:
    if diagnostic.code in _EXPLICIT:
        return _EXPLICIT[diagnostic.code]

    path = diagnostic.file.replace("\\", "/").lstrip("./")
    if path.startswith("lib/ui/"):
        return "genui"
    if path.startswith("lib/providers/") or path == "lib/main.dart":
        return "logic"
    # pubspec.yaml, DESIGN.md, generated platform folders, unknown locations.
    return "fatal"


def route_for(diagnostics: list[Diagnostic]) -> str:
    """Pick the next node for a batch of diagnostics.

    GenUI wins a mixed batch: the graph flows genui -> logic -> qa anyway, so one
    pass through GenUI gives both agents a turn before the next analysis.
    """
    owners = {owner_of(d) for d in diagnostics}
    if "fatal" in owners:
        return "fail"
    if "genui" in owners:
        return "repair_ui"
    return "repair_logic"


def unfixable(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    return [d for d in diagnostics if owner_of(d) == "fatal"]
