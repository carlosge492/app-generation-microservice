"""Does the generated app match the PRD that was paid for?

`flutter analyze` grades syntax and types. It is completely silent on whether the
code implements the spec: a PRD asking for a Cupertino app with four screens can
compile perfectly as a Material app with one. That gap is invisible to every
check we had before this module.

These assertions are deliberately naming-agnostic. They search the generated
sources for evidence of each PRD requirement rather than demanding exact file
paths, so they validate the *substance* and stay honest when a model names things
differently from `TemplateGenerator`.
"""

from __future__ import annotations

import re

from src.ports.analyzer import Diagnostic
from src.ports.templates import camel, pascal, snake
from src.prd.schema import PRD

APP_WIDGET = {"material": "MaterialApp", "cupertino": "CupertinoApp"}

# A route entry in any form: '/foo', "/foo", or a named-route constant.
def _route_present(body: str, screen_id: str) -> bool:
    slug = snake(screen_id)
    return bool(re.search(rf"""['"]/?{re.escape(slug)}['"]""", body))


def _ui_blob(files: dict[str, str]) -> str:
    return "\n".join(v for k, v in files.items() if k.startswith("lib/ui/"))


def _logic_blob(files: dict[str, str]) -> str:
    return "\n".join(
        v for k, v in files.items()
        if k.startswith("lib/providers/") or k == "lib/main.dart"
    )


def check_conformance(prd: PRD, files: dict[str, str]) -> list[Diagnostic]:
    """Assert the generated tree implements the PRD. Offline; no toolchain."""
    out: list[Diagnostic] = []
    ui = _ui_blob(files)
    logic = _logic_blob(files)

    out += _check_theme(prd, ui)
    out += _check_screens(prd, ui, files)
    out += _check_models(prd, logic)
    out += _check_form_fields(prd, files)
    return out


def _check_theme(prd: PRD, ui: str) -> list[Diagnostic]:
    expected = APP_WIDGET[prd.theme]
    if expected in ui:
        return []
    wrong = next((w for w in APP_WIDGET.values() if w != expected and w in ui), None)
    detail = f"found {wrong} instead" if wrong else "found no root app widget at all"
    return [Diagnostic(
        "error", "lib/ui/app.dart", 0, "theme_mismatch",
        f"PRD requests the {prd.theme!r} theme, so the root widget must be "
        f"{expected}; {detail}",
    )]


def _check_screens(prd: PRD, ui: str, files: dict[str, str]) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    for screen in prd.screens:
        # A widget class carrying the screen's name, however the file is named.
        if not re.search(rf"\bclass\s+\w*{pascal(screen.id)}\w*\b", ui):
            out.append(Diagnostic(
                "error", "lib/ui/", 0, "missing_screen",
                f"PRD declares screen {screen.id!r} ({screen.title!r}) but no "
                f"matching widget class was generated",
            ))
            continue
        if not _route_present(ui, screen.id):
            out.append(Diagnostic(
                "error", "lib/ui/app.dart", 0, "unreachable_screen",
                f"screen {screen.id!r} exists but is not registered as a route, "
                f"so nothing can navigate to it",
            ))
    return out


def _check_models(prd: PRD, logic: str) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    for model in prd.models:
        if not re.search(rf"\b{camel(model.name)}\w*Provider\b", logic):
            out.append(Diagnostic(
                "error", "lib/providers/", 0, "missing_provider",
                f"PRD declares model {model.name!r} but no provider for it was "
                f"generated in lib/providers/",
            ))
            continue
        # The collection name is the contract with Firestore; a typo here is
        # silent at compile time and catastrophic at runtime.
        if f"'{model.collection}'" not in logic and f'"{model.collection}"' not in logic:
            out.append(Diagnostic(
                "error", "lib/providers/", 0, "wrong_collection",
                f"model {model.name!r} must read the Firestore collection "
                f"{model.collection!r}, which appears nowhere in lib/providers/",
            ))
    return out


def _check_form_fields(prd: PRD, files: dict[str, str]) -> list[Diagnostic]:
    """A form that silently drops a field still compiles and still analyses clean."""
    out: list[Diagnostic] = []
    models = {m.name: m for m in prd.models}

    for screen in prd.screens:
        if screen.kind != "form" or not screen.model:
            continue
        model = models.get(screen.model)
        if model is None:
            continue
        expected = screen.fields or model.fields
        # Search the whole UI: the screen may legitimately be split across files.
        body = _ui_blob(files)
        for field in expected:
            if not re.search(rf"['\"]{re.escape(field.name)}['\"]", body):
                out.append(Diagnostic(
                    "error", "lib/ui/", 0, "missing_form_field",
                    f"form screen {screen.id!r} must capture field "
                    f"{field.name!r} ({field.label!r}), which no input references",
                ))
    return out
