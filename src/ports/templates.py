"""Deterministic, offline implementation of `CodeGenerator`.

Emits the pre-approved Flutter templates described in CLAUDE.md §3 ("strict over
clever"). No credentials, no network, no model calls — so the whole loop is
runnable and testable today. `AnthropicGenerator` produces the same file shapes
with a real model behind it.

Templates use `string.Template` with UPPERCASE keys so Dart's own `$name` /
`${expr}` interpolation passes through `safe_substitute` untouched.
"""

from __future__ import annotations

import re
from string import Template

import yaml

from src.ports.analyzer import Diagnostic
from src.ports.generator import Plan
from src.prd.schema import PRD, DataModel, Screen

# --------------------------------------------------------------------------- #
# Naming helpers
# --------------------------------------------------------------------------- #


def snake(text: str) -> str:
    text = re.sub(r"[^0-9a-zA-Z]+", "_", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"_+", "_", text).strip("_").lower() or "app"


def pascal(text: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in snake(text).split("_"))


def camel(text: str) -> str:
    out = pascal(text)
    return out[:1].lower() + out[1:]


def yaml_scalar(value: str) -> str:
    """Always-quote a YAML scalar.

    PRD text is arbitrary: a description like "capture app: log offline" has an
    unquoted colon that makes pubspec.yaml unparseable, and `flutter analyze`
    dies before it reaches any Dart. Single-quoted style escapes only `'`.
    """
    collapsed = " ".join(value.split())
    return "'" + collapsed.replace("'", "''") + "'"


def repair_pubspec_description(pubspec: str) -> str:
    """Quote an unquoted `description:` if that is what makes the manifest invalid.

    Our own templates learned this in Phase 1 (`yaml_scalar`), but a model
    writing the manifest rediscovers it: "Auth-first app: sign in, browse" has a
    colon, so the YAML is unparseable and `flutter analyze` aborts with a scanner
    stack trace. The manifest is planning output and therefore frozen, so nothing
    downstream can repair it — but this is punctuation, not design, and it is the
    single most common way a generated pubspec breaks.

    Deliberately narrow: if quoting the description does not make it parse, the
    original is returned unchanged and conformance reports `unparseable_pubspec`.
    """
    try:
        yaml.safe_load(pubspec)
        return pubspec
    except yaml.YAMLError:
        pass

    lines = pubspec.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("description:"):
            value = line[len("description:"):].strip()
            if value and value[0] not in "'\"":
                lines[i] = "description: " + yaml_scalar(value)
    repaired = "\n".join(lines) + "\n"

    try:
        yaml.safe_load(repaired)
    except yaml.YAMLError:
        return pubspec  # broken some other way; let conformance report it
    return repaired


def ensure_lint_dependency(pubspec: str, version: str = "^4.0.0") -> str:
    """Guarantee pubspec declares the lint package our analysis_options needs.

    We inject analysis_options.yaml ourselves, so depending on a generator to
    remember the matching dev_dependency makes our own lint guarantees hostage
    to someone else's memory. The first live model run forgot it, the include
    silently failed, and every lint-based check stopped applying.
    """
    if "flutter_lints" in pubspec:
        return pubspec

    block = f"  flutter_lints: {version}"
    lines = pubspec.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "dev_dependencies:":
            lines.insert(i + 1, block)
            break
    else:
        # No dev_dependencies section at all: append one as a new top-level key.
        lines += ["", "dev_dependencies:", block]

    return "\n".join(lines) + "\n"


def dart_string(value: str) -> str:
    """Escape a PRD string for a single-quoted Dart literal.

    Sibling of `yaml_scalar`, and the same class of bug one layer down: a screen
    titled "Ana's Café" emits `Text('Ana's Café')`, where the apostrophe closes
    the literal and everything after it is parsed as code. A `$` is worse — it
    silently becomes Dart string interpolation rather than a syntax error.
    Backslash must be escaped first or it double-escapes the others.
    """
    return (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("$", "\\$")
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def dart_type(field_type: str) -> str:
    return {"text": "String", "number": "num", "bool": "bool", "date": "DateTime"}[field_type]


def dart_default(field_type: str) -> str:
    return {"text": "''", "number": "0", "bool": "false", "date": "DateTime.now()"}[field_type]


def screen_class(screen: Screen) -> str:
    return f"{pascal(screen.id)}Screen"


def screen_file(screen: Screen) -> str:
    return f"lib/ui/{snake(screen.id)}_screen.dart"


def provider_file(model: DataModel) -> str:
    return f"lib/providers/{snake(model.name)}_providers.dart"


def list_provider(model: DataModel) -> str:
    return f"{camel(model.name)}ListProvider"


def controller_provider(model: DataModel) -> str:
    return f"{camel(model.name)}ControllerProvider"


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

PUBSPEC = Template("""name: $PKG
description: $DESCRIPTION
publish_to: 'none'
version: 0.1.0+1

environment:
  sdk: '>=3.5.0 <4.0.0'

dependencies:
  flutter:
    sdk: flutter
  flutter_riverpod: ^2.5.1
  firebase_core: ^3.6.0
  cloud_firestore: ^5.4.0
$EXTRA_DEPS
dev_dependencies:
  flutter_test:
    sdk: flutter
  flutter_lints: ^4.0.0

flutter:
  uses-material-design: true
""")

# Without this file `flutter_lints` sits in dev_dependencies and does nothing —
# the analyzer runs with default rules only, so a green analysis says far less
# than it appears to. CI is stricter than the default rule set.
ANALYSIS_OPTIONS = """include: package:flutter_lints/flutter.yaml

analyzer:
  errors:
    # The generated tree is machine-written; treat sloppiness as fatal so the
    # QA subagent sees it rather than a human reviewer.
    unused_import: error
    unused_local_variable: error
    unused_element: error
    unused_field: error
    dead_code: error
"""

DESIGN = Template("""# DESIGN.md — $APP_NAME

> Frozen once the GenUI phase begins. Changes flow downstream only.

$DESCRIPTION

## Package

- Flutter package: `$PKG`
- Application id: `$PACKAGE_NAME`
- Theme: $THEME (Material 3, standard widgets only)
- Auth required: $AUTH

## Data models

$MODELS

## Screens

$SCREENS

## Ownership boundaries

| Path | Owner |
| --- | --- |
| `lib/ui/**` | GenUI subagent — widget tree only, no business logic |
| `lib/providers/**` | Logic subagent — Riverpod state + Firebase |
| `lib/main.dart` | Logic subagent — composition root |
| `pubspec.yaml` | Planning subagent |

State is Riverpod throughout. `setState` is permitted only for a strictly
localized animation toggle, marked `// localized animation toggle`.
""")

APP_DART = Template("""import 'package:flutter/material.dart';

$IMPORTS

class $APP_CLASS extends StatelessWidget {
  const $APP_CLASS({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: '$TITLE',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorSchemeSeed: Colors.indigo,
      ),
      initialRoute: '$INITIAL_ROUTE',
      routes: {
$ROUTES
      },
    );
  }
}
""")

# --- Cupertino variants ---------------------------------------------------- #
# A CupertinoApp does not provide MaterialLocalizations, so Material widgets
# (Scaffold, AppBar, ListTile) throw at runtime inside one. The Cupertino family
# is therefore a full parallel set, not a swap of the root widget alone.

CUPERTINO_APP = Template("""import 'package:flutter/cupertino.dart';

$IMPORTS

class $APP_CLASS extends StatelessWidget {
  const $APP_CLASS({super.key});

  @override
  Widget build(BuildContext context) {
    return CupertinoApp(
      title: '$TITLE',
      debugShowCheckedModeBanner: false,
      theme: const CupertinoThemeData(
        primaryColor: CupertinoColors.activeBlue,
      ),
      initialRoute: '$INITIAL_ROUTE',
      routes: {
$ROUTES
      },
    );
  }
}
""")

CUPERTINO_LIST_SCREEN = Template("""import 'package:flutter/cupertino.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/$PROVIDER_MODULE.dart';

class $CLASS extends ConsumerWidget {
  const $CLASS({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final rows = ref.watch($LIST_PROVIDER);
    return CupertinoPageScaffold(
      navigationBar: CupertinoNavigationBar(
        middle: const Text('$TITLE'),
        trailing: CupertinoButton(
          padding: EdgeInsets.zero,
          onPressed: () => ref.read($CONTROLLER_PROVIDER.notifier).createDraft(),
          child: const Icon(CupertinoIcons.add),
        ),
      ),
      child: SafeArea(
        child: rows.when(
          data: (items) => ListView.builder(
            itemCount: items.length,
            itemBuilder: (context, index) {
              final item = items[index];
              return CupertinoListTile(
                title: Text(item.$PRIMARY_FIELD.toString()),
                subtitle: Text(item.id),
              );
            },
          ),
          loading: () => const Center(child: CupertinoActivityIndicator()),
          error: (error, stackTrace) => Center(child: Text('Failed to load: $error')),
        ),
      ),
    );
  }
}
""")

CUPERTINO_FORM_SCREEN = Template("""import 'package:flutter/cupertino.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/$PROVIDER_MODULE.dart';

class $CLASS extends ConsumerWidget {
  const $CLASS({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final controller = ref.read($CONTROLLER_PROVIDER.notifier);
    return CupertinoPageScaffold(
      navigationBar: CupertinoNavigationBar(middle: const Text('$TITLE')),
      child: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
$FIELDS
            const SizedBox(height: 24),
            CupertinoButton.filled(
              onPressed: controller.submit,
              child: const Text('Save'),
            ),
          ],
        ),
      ),
    );
  }
}
""")

CUPERTINO_PLAIN_SCREEN = Template("""import 'package:flutter/cupertino.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

class $CLASS extends ConsumerWidget {
  const $CLASS({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return CupertinoPageScaffold(
      navigationBar: CupertinoNavigationBar(middle: const Text('$TITLE')),
      child: const SafeArea(
        child: Center(
          child: Padding(
            padding: EdgeInsets.all(24),
            child: Text('$TITLE'),
          ),
        ),
      ),
    );
  }
}
""")

MAIN_DART = Template("""import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/$FRAMEWORK.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'ui/app.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Firebase.initializeApp();
  runApp(const ProviderScope(child: $APP_CLASS()));
}
""")

LIST_SCREEN = Template("""import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/$PROVIDER_MODULE.dart';

class $CLASS extends ConsumerWidget {
  const $CLASS({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final rows = ref.watch($LIST_PROVIDER);
    return Scaffold(
      appBar: AppBar(title: const Text('$TITLE')),
      body: rows.when(
        data: (items) => ListView.separated(
          itemCount: items.length,
          separatorBuilder: (context, index) => const Divider(height: 1),
          itemBuilder: (context, index) {
            final item = items[index];
            return ListTile(
              title: Text(item.$PRIMARY_FIELD.toString()),
              subtitle: Text(item.id),
              onTap: () {},
            );
          },
        ),
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (error, stackTrace) => Center(child: Text('Failed to load: $error')),
      ),
      floatingActionButton: FloatingActionButton(
        onPressed: () => ref.read($CONTROLLER_PROVIDER.notifier).createDraft(),
        child: const Icon(Icons.add),
      ),
    );
  }
}
""")

FORM_SCREEN = Template("""import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/$PROVIDER_MODULE.dart';

class $CLASS extends ConsumerWidget {
  const $CLASS({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final controller = ref.read($CONTROLLER_PROVIDER.notifier);
    return Scaffold(
      appBar: AppBar(title: const Text('$TITLE')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
$FIELDS
          const SizedBox(height: 24),
          FilledButton(
            onPressed: controller.submit,
            child: const Text('Save'),
          ),
        ],
      ),
    );
  }
}
""")

PLAIN_SCREEN = Template("""import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

class $CLASS extends ConsumerWidget {
  const $CLASS({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Scaffold(
      appBar: AppBar(title: const Text('$TITLE')),
      body: const Center(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Text('$TITLE'),
        ),
      ),
    );
  }
}
""")

PROVIDERS = Template("""import 'package:cloud_firestore/cloud_firestore.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

class $CLASS {
  const $CLASS({
    required this.id,
$CTOR_PARAMS
  });

  final String id;
$FIELD_DECLS

  factory $CLASS.fromSnapshot(DocumentSnapshot<Map<String, dynamic>> doc) {
    final data = doc.data() ?? <String, dynamic>{};
    return $CLASS(
      id: doc.id,
$FROM_MAP
    );
  }

  Map<String, dynamic> toMap() {
    return <String, dynamic>{
$TO_MAP
    };
  }
}

final ${VAR}CollectionProvider =
    Provider<CollectionReference<Map<String, dynamic>>>((ref) {
  return FirebaseFirestore.instance.collection('$COLLECTION');
});

final $LIST_PROVIDER = StreamProvider<List<$CLASS>>((ref) {
  final collection = ref.watch(${VAR}CollectionProvider);
  return collection.snapshots().map(
        (snapshot) => snapshot.docs.map($CLASS.fromSnapshot).toList(),
      );
});

final $CONTROLLER_PROVIDER =
    StateNotifierProvider<$CONTROLLER_CLASS, AsyncValue<void>>((ref) {
  return $CONTROLLER_CLASS(ref);
});

class $CONTROLLER_CLASS extends StateNotifier<AsyncValue<void>> {
  $CONTROLLER_CLASS(this._ref) : super(const AsyncValue.data(null));

  final Ref _ref;
  final Map<String, dynamic> _draft = <String, dynamic>{};

  void createDraft() {
    _draft.clear();
  }

  void update(String field, dynamic value) {
    _draft[field] = value;
  }

  Future<void> submit() async {
    state = const AsyncValue.loading();
    try {
      await _ref.read(${VAR}CollectionProvider).add(Map<String, dynamic>.from(_draft));
      _draft.clear();
      state = const AsyncValue.data(null);
    } catch (error, stackTrace) {
      state = AsyncValue.error(error, stackTrace);
    }
  }

  Future<void> delete(String id) async {
    await _ref.read(${VAR}CollectionProvider).doc(id).delete();
  }
}
""")


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #


class TemplateGenerator:
    """Offline `CodeGenerator`.

    `fault` deliberately breaks the first GenUI pass so the QA -> Logic repair
    loop can be exercised without a live model. Used by the tests.
    """

    def __init__(self, fault: str | None = None) -> None:
        self.fault = fault

    # -- planning subagent -------------------------------------------------- #

    def plan(self, prd: PRD) -> Plan:
        pkg = snake(prd.app_name)
        models_md = "\n".join(
            f"- **{m.name}** -> `{m.collection}` "
            f"({', '.join(f'{f.name}: {dart_type(f.type)}' for f in m.fields) or 'no fields'})"
            for m in prd.models
        ) or "_none_"
        screens_md = "\n".join(
            f"- `{s.id}` ({s.kind}) — {s.title}"
            + (f", backed by `{s.model}`" if s.model else "")
            for s in prd.screens
        )
        design = DESIGN.safe_substitute(
            APP_NAME=prd.app_name,
            DESCRIPTION=prd.description or "_No description supplied._",
            PKG=pkg,
            PACKAGE_NAME=prd.package_name,
            THEME=prd.theme,
            AUTH="yes" if prd.auth else "no",
            MODELS=models_md,
            SCREENS=screens_md,
        )
        pubspec = PUBSPEC.safe_substitute(
            PKG=pkg,
            DESCRIPTION=yaml_scalar(prd.description or prd.app_name),
            # CupertinoIcons resolves at compile time but renders nothing
            # without the font package.
            EXTRA_DEPS="  cupertino_icons: ^1.0.8\n" if prd.theme == "cupertino" else "",
        )
        return Plan(design_md=design, pubspec=pubspec, analysis_options=ANALYSIS_OPTIONS)

    # -- genui subagent ----------------------------------------------------- #

    def build_ui(
        self, prd: PRD, design_md: str, diagnostics: list[Diagnostic] | None = None
    ) -> dict[str, str]:
        diagnostics = diagnostics or []
        models = {m.name: m for m in prd.models}
        files: dict[str, str] = {}

        for screen in prd.screens:
            model = models.get(screen.model) if screen.model else None
            files[screen_file(screen)] = self._render_screen(screen, model, prd.theme)

        app_class = f"{pascal(prd.app_name)}App"
        imports = "\n".join(
            f"import '{snake(s.id)}_screen.dart';" for s in prd.screens
        )
        routes = "\n".join(
            f"        '/{snake(s.id)}': (context) => const {screen_class(s)}(),"
            for s in prd.screens
        )
        app_template = CUPERTINO_APP if prd.theme == "cupertino" else APP_DART
        files["lib/ui/app.dart"] = app_template.safe_substitute(
            IMPORTS=imports,
            APP_CLASS=app_class,
            TITLE=dart_string(prd.app_name),
            INITIAL_ROUTE=f"/{snake(prd.screens[0].id)}",
            ROUTES=routes,
        )

        # Faults are injected on the first pass only. A repair pass regenerates
        # cleanly, which is what a real GenUI repair converges on.
        if not diagnostics:
            target = screen_file(prd.screens[0])
            if self.fault == "undefined_provider":
                # GenUI/Logic desync: watch a provider nobody declares. Owned by
                # the Logic subagent, per CLAUDE.md §4.
                files[target] = files[target].replace(
                    "    return Scaffold(",
                    "    ref.watch(unsyncedDraftProvider);\n    return Scaffold(",
                    1,
                )
            elif self.fault == "setstate":
                # A UI-owned violation: only GenUI can write lib/ui/ to fix it.
                files[target] = files[target].replace(
                    "    return Scaffold(",
                    "    setState(() {});\n    return Scaffold(",
                    1,
                )
        return files

    def _render_screen(
        self, screen: Screen, model: DataModel | None, theme: str = "material"
    ) -> str:
        cupertino = theme == "cupertino"
        cls = screen_class(screen)
        if model is None:
            plain = CUPERTINO_PLAIN_SCREEN if cupertino else PLAIN_SCREEN
            return plain.safe_substitute(CLASS=cls, TITLE=dart_string(screen.title))

        module = snake(model.name) + "_providers"
        if screen.kind == "form":
            if cupertino:
                fields = "\n".join(
                    "            CupertinoTextField(\n"
                    f"              placeholder: '{dart_string(f.label)}',\n"
                    "              onChanged: (value) => "
                    f"controller.update('{f.name}', value),\n"
                    "            ),"
                    for f in (screen.fields or model.fields)
                )
            else:
                fields = "\n".join(
                    "          TextFormField(\n"
                    "            decoration: const InputDecoration("
                    f"labelText: '{dart_string(f.label)}'),\n"
                    f"            onChanged: (value) => controller.update('{f.name}', value),\n"
                    "          ),"
                    for f in (screen.fields or model.fields)
                )
            form = CUPERTINO_FORM_SCREEN if cupertino else FORM_SCREEN
            return form.safe_substitute(
                CLASS=cls,
                TITLE=dart_string(screen.title),
                PROVIDER_MODULE=module,
                CONTROLLER_PROVIDER=controller_provider(model),
                FIELDS=fields,
            )

        primary = model.fields[0].name if model.fields else "id"
        listing = CUPERTINO_LIST_SCREEN if cupertino else LIST_SCREEN
        return listing.safe_substitute(
            CLASS=cls,
            TITLE=dart_string(screen.title),
            PROVIDER_MODULE=module,
            LIST_PROVIDER=list_provider(model),
            CONTROLLER_PROVIDER=controller_provider(model),
            PRIMARY_FIELD=primary,
        )

    # -- logic subagent ----------------------------------------------------- #

    def wire_logic(
        self,
        prd: PRD,
        design_md: str,
        ui_files: dict[str, str],
        diagnostics: list[Diagnostic],
    ) -> dict[str, str]:
        files: dict[str, str] = {}
        for model in prd.models:
            files[provider_file(model)] = self._render_providers(model)

        files["lib/main.dart"] = MAIN_DART.safe_substitute(
            APP_CLASS=f"{pascal(prd.app_name)}App",
            FRAMEWORK="cupertino" if prd.theme == "cupertino" else "material",
        )

        # No fabricated providers here, deliberately.
        #
        # An earlier version answered `undefined_provider` by declaring a
        # throwaway `StateProvider<Map<String, dynamic>>` named after whatever
        # the UI referenced. That silenced the diagnostic without making the app
        # any more correct: a green build backed by a provider that reads
        # nothing and means nothing. It made the repair loop look like it worked
        # while measuring only its own output.
        #
        # The Logic subagent's honest answer is the providers the PRD implies,
        # which is exactly what it generates above. If the UI references
        # something with no basis in the PRD, Logic genuinely cannot fix it, the
        # diagnostic survives, and the router escalates to GenUI to stop
        # referencing it. See `make_router`.
        return files

    def _render_providers(self, model: DataModel) -> str:
        cls = pascal(model.name)
        var = camel(model.name)
        ctor_params = "\n".join(
            f"    required this.{f.name}," for f in model.fields
        )
        field_decls = "\n".join(
            f"  final {dart_type(f.type)} {f.name};" for f in model.fields
        )
        from_map = "\n".join(
            f"      {f.name}: (data['{f.name}'] as {dart_type(f.type)}?) "
            f"?? {dart_default(f.type)},"
            for f in model.fields
        )
        to_map = "\n".join(f"      '{f.name}': {f.name}," for f in model.fields)
        return PROVIDERS.safe_substitute(
            CLASS=cls,
            VAR=var,
            COLLECTION=dart_string(model.collection),
            LIST_PROVIDER=list_provider(model),
            CONTROLLER_PROVIDER=controller_provider(model),
            CONTROLLER_CLASS=f"{cls}Controller",
            CTOR_PARAMS=ctor_params,
            FIELD_DECLS=field_decls,
            FROM_MAP=from_map,
            TO_MAP=to_map,
        )
