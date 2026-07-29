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

import yaml

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
    """Everything under lib/ that is not the widget tree.

    Models and Firestore access legitimately live in lib/models/ or
    lib/services/ rather than lib/providers/, so restricting this to
    lib/providers/ made `missing_provider` and `wrong_collection` fire against
    perfectly correct code.
    """
    return "\n".join(
        v for k, v in files.items()
        if k.startswith("lib/") and not k.startswith("lib/ui/")
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
    out += _check_navigation_reachable(prd, files)
    out += _check_unwired_declarations(files)
    out += _check_pubspec_parses(files)
    out += _check_lint_package_installed(files)
    out += _check_single_entrypoint(files)
    out += _check_duplicate_declarations(files)
    return out


def _check_navigation_reachable(prd: PRD, files: dict[str, str]) -> list[Diagnostic]:
    """A declared `navigate` action must actually be able to go somewhere.

    The template generator listed every screen in the routes table and never
    pushed one, so `/capture` and `/settings` existed and nothing could reach
    them. Nothing caught it: each screen built in isolation, the routes table
    was valid Dart, and neither the analyzer nor the smoke tests have an opinion
    about whether a route is ever navigated to. The app was a form nobody could
    open.

    The check is deliberately loose about *how*. `Navigator.pushNamed(context,
    '/capture')`, a `MaterialPageRoute` building `CaptureScreen`, or a router's
    `go('/capture')` are all fine — what it will not accept is the target
    appearing only in the routes table that declares it, which is the shape the
    bug had.
    """
    out: list[Diagnostic] = []
    routes_table = {
        path for path, body in files.items()
        if path.startswith("lib/ui/") and re.search(r"\broutes\s*:", body)
    }

    for screen in prd.screens:
        for action in screen.actions:
            if action.kind != "navigate" or not action.target:
                continue
            slug = snake(action.target)
            widget = f"{pascal(action.target)}Screen"

            # Somewhere *else* has to do the navigating. Excluded: the routes
            # table, which only declares the destination, and the destination's
            # own file — whose `class CaptureScreen` and `const CaptureScreen(`
            # otherwise match and make every unreachable screen look reachable.
            declares_target = re.compile(rf"\bclass\s+{re.escape(widget)}\b")
            reachable_from = "\n".join(
                body for path, body in files.items()
                if path.startswith("lib/ui/")
                and path not in routes_table
                and not declares_target.search(body)
            )

            if re.search(rf"""['"]/?{re.escape(slug)}['"]""", reachable_from):
                continue
            if re.search(rf"\b{re.escape(widget)}\s*\(", reachable_from):
                continue
            out.append(Diagnostic(
                "error", f"lib/ui/{snake(screen.id)}_screen.dart", 0,
                "unreachable_screen",
                f"screen '{screen.id}' declares a navigate action "
                f"'{action.name}' to '{action.target}', but nothing outside the "
                f"routes table navigates there — the target is unreachable",
            ))
    return out


def _check_pubspec_parses(files: dict[str, str]) -> list[Diagnostic]:
    """pubspec.yaml must be valid YAML before anything else is worth checking.

    `StubAnalyzer` already does this, but it is skipped under `--analyzer dart`,
    and the real toolchain does not degrade gracefully: a malformed pubspec makes
    `flutter analyze` abort with a YAML scanner stack trace and exit 255, which
    surfaces as a toolchain failure rather than the model's mistake. Duplicating
    the check here keeps it analyzer-independent.
    """
    text = files.get("pubspec.yaml")
    if text is None:
        return [Diagnostic("error", "pubspec.yaml", 0, "missing_pubspec",
                           "no pubspec.yaml was produced")]
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [Diagnostic("error", "pubspec.yaml", 0, "unparseable_pubspec",
                           f"pubspec.yaml is not valid YAML: {' '.join(str(exc).split())}")]
    if not isinstance(parsed, dict):
        return [Diagnostic("error", "pubspec.yaml", 0, "unparseable_pubspec",
                           "pubspec.yaml did not parse to a YAML mapping")]
    return []


def _check_lint_package_installed(files: dict[str, str]) -> list[Diagnostic]:
    """analysis_options.yaml is inert unless the pubspec installs the lint package.

    Found on the first live model run: the generated pubspec omitted
    flutter_lints, so the include failed, the analyser fell back to default
    rules, and every lint-based guarantee quietly stopped applying while the
    build still went green.
    """
    options = files.get("analysis_options.yaml", "")
    pubspec = files.get("pubspec.yaml", "")
    if "flutter_lints" not in options or "flutter_lints" in pubspec:
        return []
    return [Diagnostic(
        "error", "pubspec.yaml", 0, "lints_not_installed",
        "analysis_options.yaml includes package:flutter_lints but pubspec.yaml "
        "never declares it, so every lint rule is silently disabled",
    )]


def _check_single_entrypoint(files: dict[str, str]) -> list[Diagnostic]:
    """`main()` belongs in lib/main.dart and nowhere else.

    A second `main()` under lib/ui/ is a lane violation our path-based check
    cannot see — the file is in GenUI's lane, but bootstrapping the app is
    Logic's job. Dart compiles it happily; the app just has two shells.
    """
    out: list[Diagnostic] = []
    for path, body in sorted(files.items()):
        if not path.startswith("lib/") or path == "lib/main.dart":
            continue
        if re.search(r"^(?:Future<void>\s+|void\s+)main\s*\(", body, re.M):
            out.append(Diagnostic(
                "error", path, 0, "misplaced_entrypoint",
                "main() is declared here as well as in lib/main.dart; the "
                "composition root belongs solely to lib/main.dart",
            ))
    return out


def _check_duplicate_declarations(files: dict[str, str]) -> list[Diagnostic]:
    """The same top-level name declared in two files.

    Two subagents each writing their own `MinimalApp` is not a compile error —
    they are separate libraries — but the app ends up with two divergent copies
    of its own shell, and only one of them is reachable.
    """
    seen: dict[str, str] = {}
    out: list[Diagnostic] = []
    for path, body in sorted(files.items()):
        if not path.startswith("lib/"):
            continue
        for name in sorted(_top_level_names(body)):
            if name in _EXEMPT:
                continue
            if name in seen:
                out.append(Diagnostic(
                    "error", path, 0, "duplicate_declaration",
                    f"{name!r} is also declared in {seen[name]}; two copies of the "
                    f"same declaration means one of them is dead",
                ))
            else:
                seen[name] = path
    return out


# Top-level declarations only. Matched per line rather than with a multiline
# regex: `\s` inside a character class silently consumes indentation, which made
# an earlier version flag every class *method* as top-level.
_CLASS_LINE = re.compile(r"(?:class|mixin|enum|extension)\s+([A-Za-z]\w*)")
# Requires the opening brace but not end-of-line, so a single-line body
# (`String f(String s) { return s; }`) is matched as well as a multi-line one.
_FUNC_LINE = re.compile(r"[\w<>,?\[\]]+\s+([a-z]\w*)\s*\([^)]*\)\s*(?:async\s*)?\{")
_VAR_LINE = re.compile(r"(?:final|const)\s+(?:[\w<>,?\[\]]+\s+)?([a-z]\w*)\s*=")

# `main` is called by the Flutter engine, never from Dart source.
_ENTRYPOINTS = {"main"}

# Serialization and value-semantics boilerplate on a data class.
#
# These are architectural contracts, not dead code. `toMap` exists so the model
# *can* be written to Firestore, whether or not this particular PRD happens to
# include a form screen that writes one; `fromSnapshot` is the read half of the
# same contract. Judging them by reference count measures the current PRD's
# feature set, not whether the data layer is correctly formed.
#
# Currently belt-and-braces: the matcher only scans column 0, so class methods
# never reach this filter. It is written down because the obvious extension of
# this check is to methods, and that is precisely when omitting it would start
# deleting the data layer.
_MODEL_BOILERPLATE = {
    "toMap", "fromMap", "toJson", "fromJson", "fromSnapshot",
    "copyWith", "toString", "hashCode",
}

_EXEMPT = _ENTRYPOINTS | _MODEL_BOILERPLATE


def _top_level_names(body: str) -> set[str]:
    """Names declared at column 0. A leading space means it is nested."""
    names: set[str] = set()
    for line in body.splitlines():
        if not line or line[0].isspace() or line.lstrip().startswith(("//", "import", "@")):
            continue
        for pattern in (_CLASS_LINE, _FUNC_LINE, _VAR_LINE):
            found = pattern.match(line)
            if found:
                names.add(found.group(1))
                break
    return names


def _check_unwired_declarations(files: dict[str, str]) -> list[Diagnostic]:
    """Public top-level declarations that nothing references.

    `dart analyze` reports `unused_element` for *private* declarations only — it
    cannot know whether an external package imports a public one. A generated
    single-app tree has no external importers, so that reasoning does not apply
    and the blind spot is real: a model that writes `String formatSubtitle(...)`
    and never calls it produces a completely clean analysis.

    That is the more likely hallucination shape, too, since nothing pushes a
    model towards the `_` prefix that would have made the analyzer care.
    """
    lib = {k: v for k, v in files.items() if k.startswith("lib/")}
    blob = "\n".join(lib.values())
    out: list[Diagnostic] = []

    for path, body in sorted(lib.items()):
        for name in sorted(_top_level_names(body)):
            if name in _EXEMPT or name.startswith("_"):
                continue
            # The declaration itself is one occurrence; a wired declaration has
            # at least one more.
            if len(re.findall(rf"\b{re.escape(name)}\b", blob)) < 2:
                line = next(
                    (n for n, ln in enumerate(body.splitlines(), 1) if name in ln), 0
                )
                out.append(Diagnostic(
                    "error", path, line, "unwired_declaration",
                    f"{name!r} is declared but never referenced anywhere in lib/; "
                    f"either wire it up or do not generate it",
                ))
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
    """Every PRD screen must exist as a widget and be wired into the app.

    Reachability is judged by whether the widget class is referenced from
    somewhere other than the file declaring it — not by looking for a route
    string. A single-screen app reached via `home: const ArticlesScreen()` is
    perfectly reachable, and so is one using `onGenerateRoute`, a router
    package, or route-name constants. Demanding a literal `'/articles'` flagged
    all of those as broken.
    """
    ui_files = {k: v for k, v in files.items() if k.startswith("lib/ui/")}
    out: list[Diagnostic] = []

    for screen in prd.screens:
        pattern = rf"\bclass\s+(\w*{pascal(screen.id)}\w*)\b"
        declaring = next(
            ((path, re.search(pattern, body).group(1))
             for path, body in sorted(ui_files.items()) if re.search(pattern, body)),
            None,
        )
        if declaring is None:
            out.append(Diagnostic(
                "error", "lib/ui/", 0, "missing_screen",
                f"PRD declares screen {screen.id!r} ({screen.title!r}) but no "
                f"matching widget class was generated",
            ))
            continue

        path, cls = declaring
        referenced_elsewhere = any(
            other != path and re.search(rf"\b{re.escape(cls)}\b", body)
            for other, body in ui_files.items()
        )
        if not referenced_elsewhere:
            out.append(Diagnostic(
                "error", "lib/ui/app.dart", 0, "unreachable_screen",
                f"screen {screen.id!r} is declared as {cls!r} in {path} but nothing "
                f"else references it, so the app can never show it",
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
            # A field counts as captured if its name appears as part of an
            # identifier, or its label appears as a string. Requiring the
            # literal `'name'` assumed TemplateGenerator's
            # `controller.update('name', value)` idiom; an ordinary Flutter form
            # uses `TextEditingController _nameController` and
            # `labelText: 'Name'`, and never quotes the field name at all.
            by_identifier = re.search(
                rf"\b_*{re.escape(field.name)}\w*\b", body, re.IGNORECASE
            )
            by_label = (
                f"'{field.label}'" in body or f'"{field.label}"' in body
            )
            if not (by_identifier or by_label):
                out.append(Diagnostic(
                    "error", "lib/ui/", 0, "missing_form_field",
                    f"form screen {screen.id!r} must capture field "
                    f"{field.name!r} ({field.label!r}), which no input references",
                ))
    return out
