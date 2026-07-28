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

dev_dependencies:
  flutter_test:
    sdk: flutter
  flutter_lints: ^4.0.0

flutter:
  uses-material-design: true
""")

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

MAIN_DART = Template("""import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/material.dart';
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
            DESCRIPTION=prd.description or prd.app_name,
        )
        return Plan(design_md=design, pubspec=pubspec)

    # -- genui subagent ----------------------------------------------------- #

    def build_ui(self, prd: PRD, design_md: str) -> dict[str, str]:
        models = {m.name: m for m in prd.models}
        files: dict[str, str] = {}

        for screen in prd.screens:
            model = models.get(screen.model) if screen.model else None
            files[screen_file(screen)] = self._render_screen(screen, model)

        app_class = f"{pascal(prd.app_name)}App"
        imports = "\n".join(
            f"import '{snake(s.id)}_screen.dart';" for s in prd.screens
        )
        routes = "\n".join(
            f"        '/{snake(s.id)}': (context) => const {screen_class(s)}(),"
            for s in prd.screens
        )
        files["lib/ui/app.dart"] = APP_DART.safe_substitute(
            IMPORTS=imports,
            APP_CLASS=app_class,
            TITLE=prd.app_name,
            INITIAL_ROUTE=f"/{snake(prd.screens[0].id)}",
            ROUTES=routes,
        )

        if self.fault == "undefined_provider":
            # Simulate the GenUI/Logic desync: watch a provider nobody declares.
            target = screen_file(prd.screens[0])
            files[target] = files[target].replace(
                "    return Scaffold(",
                "    ref.watch(unsyncedDraftProvider);\n    return Scaffold(",
                1,
            )
        return files

    def _render_screen(self, screen: Screen, model: DataModel | None) -> str:
        cls = screen_class(screen)
        if model is None:
            return PLAIN_SCREEN.safe_substitute(CLASS=cls, TITLE=screen.title)

        module = snake(model.name) + "_providers"
        if screen.kind == "form":
            fields = "\n".join(
                "          TextFormField(\n"
                f"            decoration: const InputDecoration(labelText: '{f.label}'),\n"
                f"            onChanged: (value) => controller.update('{f.name}', value),\n"
                "          ),"
                for f in (screen.fields or model.fields)
            )
            return FORM_SCREEN.safe_substitute(
                CLASS=cls,
                TITLE=screen.title,
                PROVIDER_MODULE=module,
                CONTROLLER_PROVIDER=controller_provider(model),
                FIELDS=fields,
            )

        primary = model.fields[0].name if model.fields else "id"
        return LIST_SCREEN.safe_substitute(
            CLASS=cls,
            TITLE=screen.title,
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
            APP_CLASS=f"{pascal(prd.app_name)}App"
        )

        # Repair pass: declare exactly the providers QA reported as undefined,
        # rather than regenerating the file wholesale (CLAUDE.md §4).
        missing = sorted(
            {
                m.group(1)
                for d in diagnostics
                if d.code == "undefined_provider"
                for m in [re.search(r"provider '([^']+)'", d.message)]
                if m
            }
        )
        if missing:
            patch = ["import 'package:flutter_riverpod/flutter_riverpod.dart';", ""]
            patch += [
                f"// Declared by the Logic subagent to resolve QA diagnostic "
                f"`undefined_provider` for {name!r}.\n"
                f"final {name} = StateProvider<Map<String, dynamic>>((ref) "
                "=> <String, dynamic>{});"
                for name in missing
            ]
            files["lib/providers/repair_providers.dart"] = "\n".join(patch) + "\n"

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
            COLLECTION=model.collection,
            LIST_PROVIDER=list_provider(model),
            CONTROLLER_PROVIDER=controller_provider(model),
            CONTROLLER_CLASS=f"{cls}Controller",
            CTOR_PARAMS=ctor_params,
            FIELD_DECLS=field_decls,
            FROM_MAP=from_map,
            TO_MAP=to_map,
        )
