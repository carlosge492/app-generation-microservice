"""Generated tests that walk the app's declared navigation at runtime.

`conformance.py` proves *something* navigates to every screen the PRD says is a
target. That is a static claim about text in a file. It does not prove the
destination renders, and it cannot: the route table entry
`'/capture': (context) => const CaptureScreen()` satisfies the static check
whether or not `CaptureScreen` survives being built for real.

This closes the other half, in two claims per declared navigate action:

1. **The destination renders.** Boot the app through its own `main()` — the real
   composition root, the real `ProviderScope`, the real Firebase initialisation
   — drive the app's own `Navigator` to the target and check the destination is
   on screen. Two things that were separately broken get exercised in passing:
   an app whose `main()` could not start, and screens that had only ever been
   built in isolation by the smoke tests.
2. **A user can get there by tapping.** Go to the source screen and tap its
   affordances, one per attempt, looking for one that arrives at the target.

**Why the destination is identified by its title.** Screen titles come from the
PRD, so they are the one label guaranteed to be the same whatever a generator
names its classes, files or routes. Matching a widget class would be matching
`TemplateGenerator`'s `<Id>Screen` convention, and Claude does not always use it.

**Why the route name is not taken from the generator either.** The route is
`'/<screen id>'`, derived from the PRD, and both generators independently chose
that shape. Where a generator does something else the test fails loudly rather
than silently passing, which is the right direction for a check whose whole
purpose is catching unreachable screens.

**On hunting for the button.** The first version of this module refused to,
because "tap whatever looks tappable" sounds like the kind of check that depends
on widget choices and goes flaky. Three things make it honest rather than lucky:

* *What counts as tappable is not a list of buttons.* `InkResponse` is
  underneath `TextButton`, `ElevatedButton`, `IconButton`, `FloatingActionButton`
  and `ListTile`; `CupertinoButton` and a hand-rolled `GestureDetector` cover
  the rest. Enumerating button classes would have encoded one generator's taste;
  enumerating the two or three primitives they are all built from does not.
  It also finds affordances with no text at all — the template generator's own
  list screens navigate from a `FloatingActionButton` whose only child is an
  icon, so any text-based search would have missed the one case that ships most.
* *Every attempt starts from a freshly pushed source screen*, so a tap that
  opens a dialog, mutates state or navigates somewhere else cannot change what
  the next attempt hits. That is what turns "tap things until something happens"
  from a stateful sequence into N independent one-tap experiments.
* *Arrival is route name first, title second.* `'/target'` on top of the
  navigator is unambiguous; the title is the fallback for a generator that
  pushes an unnamed `MaterialPageRoute`. The title alone would not do — a button
  labelled with the destination's title is good UI and would make the test pass
  before it tapped anything, so the title only counts if it was *not* already on
  screen.

What it still cannot prove is that a *multi-step* flow arrives: a sign-in form
that navigates only after valid credentials needs one tap and some typing, and
the PRD says nothing about what to type. Those fail here, honestly, rather than
being quietly excluded.
"""

from __future__ import annotations

import re

from src.ports.templates import snake
from src.prd.schema import PRD, Action, Screen

_PUBSPEC_NAME = re.compile(r"^name:\s*([A-Za-z_][A-Za-z0-9_]*)", re.M)

# How many affordances a single tap case tries before giving up; the reasoning
# is in the doc comment it is emitted with.
_TAP_BUDGET = 40


def _package_name(prd: PRD, files: dict[str, str]) -> str:
    match = _PUBSPEC_NAME.search(files.get("pubspec.yaml", ""))
    return match.group(1) if match else snake(prd.app_name)


def _dart_literal(text: str) -> str:
    """A single-quoted Dart string. Titles are free text from the PRD."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("$", "\\$")
        .replace("\r", "")
        .replace("\n", "\\n")
    )
    return f"'{escaped}'"


# Shared by every case in the file. Kept as one block rather than interpolated
# per case so the generated test reads like something a person would write.
_HELPERS = """
/// How many affordances a tap case tries before giving up. A list screen backed
/// by an emulator can render dozens of rows, and each attempt costs a push and
/// a settle; the failure message reports the budget so a genuine overrun cannot
/// be mistaken for "nothing here reaches the target".
const int _tapBudget = %(budget)d;

/// The app's own root [Navigator] — not one this test installed.
widgets.NavigatorState _navigator(WidgetTester tester) =>
    tester.state<widgets.NavigatorState>(find.byType(widgets.Navigator).first);

/// The name of the route currently on top, or null if it has none.
///
/// `popUntil` calls its predicate with the topmost route first and pops nothing
/// when it immediately returns true, which is the only way to read the current
/// route without owning the `MaterialApp` that created the navigator.
String? _currentRoute(WidgetTester tester) {
  String? name;
  _navigator(tester).popUntil((route) {
    name = route.settings.name;
    return true;
  });
  return name;
}

/// `pumpAndSettle`, bounded, and tolerant of a screen that never settles.
///
/// An indeterminate progress indicator animates forever, so a screen waiting on
/// data never settles and the default ten-minute timeout would be spent on
/// every attempt. A few frames is enough to see where a tap landed.
Future<void> _settle(WidgetTester tester) async {
  try {
    await tester.pumpAndSettle(
      const Duration(milliseconds: 100),
      EnginePhase.sendSemanticsUpdate,
      const Duration(seconds: 5),
    );
  } catch (_) {
    await tester.pump(const Duration(milliseconds: 600));
  }
}

/// Everything on screen that a user could plausibly tap.
///
/// Deliberately the low-level affordances rather than a list of button classes:
/// `InkResponse` is underneath `TextButton`, `ElevatedButton`, `IconButton`,
/// `FloatingActionButton` and `ListTile`, and a generator that hand-rolls a
/// button still ends up with a `GestureDetector`. Naming the buttons would be
/// encoding one generator's taste in widgets.
Finder _tappables() => find.byWidgetPredicate(
      (widget) =>
          widget is material.InkResponse ||
          widget is cupertino.CupertinoButton ||
          widget is widgets.GestureDetector,
      description: 'tappable widget',
    );

/// Show the screen [title] belongs to, and say whether it worked.
///
/// The route is assumed to be `'/<screen id>'`, which is what the PRD implies.
/// The **home** screen is the exception: `'/'` is the conventional name for it
/// in Flutter, and an app that files it there answers a push of `'/<id>'` with
/// whatever its `onGenerateRoute` returns for an unknown name — frequently a
/// placeholder rather than an error. Opus generated exactly that, a "Route not
/// found" Scaffold, so this walked onto an empty page and reported the target
/// unreachable when the app was perfectly navigable.
///
/// Popping to the first route is therefore tried first, and the named push only
/// if the wanted screen is not already the one showing.
Future<bool> _open(WidgetTester tester, String route, Finder title) async {
  _navigator(tester).popUntil((existing) => existing.isFirst);
  await _settle(tester);
  if (title.evaluate().isNotEmpty) {
    return true; // it is the home screen; there is no route to push
  }
  _navigator(tester).pushNamed(route);
  await _settle(tester);
  return title.evaluate().isNotEmpty;
}

/// Tap the affordances on [source] one at a time, looking for one that arrives
/// at [target]. Returns the empty string on success, or why it failed.
///
/// Each attempt re-opens [source] first, so every tap is an independent
/// experiment on the same starting state rather than a step in a sequence.
Future<String> _tapReaches(
  WidgetTester tester,
  String source,
  String sourceTitle,
  String target,
  Finder title,
) async {
  final sourceFinder = find.text(sourceTitle);
  if (!await _open(tester, source, sourceFinder)) {
    return 'could not get to $source to start from: neither the first route nor '
        'a push of $source shows "$sourceTitle", so this says nothing about '
        'whether $target is reachable';
  }
  final total = _tappables().evaluate().length;
  if (total == 0) {
    return 'nothing on $source is tappable, so the target cannot be reached '
        'by a user at all';
  }
  // A button labelled with the destination's title is good UI, and would make
  // this pass before it tapped anything if arrival meant "the title is on
  // screen". It only counts as arrival if it was not already there.
  final titleWasVisible = title.evaluate().isNotEmpty;

  final budget = total < _tapBudget ? total : _tapBudget;
  for (var i = 0; i < budget; i++) {
    await _open(tester, source, sourceFinder);
    final candidates = _tappables();
    if (i >= candidates.evaluate().length) {
      break;
    }
    try {
      await tester.tap(candidates.at(i), warnIfMissed: false);
    } catch (_) {
      continue; // off screen or covered: not an affordance a user could use
    }
    await _settle(tester);
    if (_currentRoute(tester) == target) {
      return '';
    }
    if (!titleWasVisible && title.evaluate().isNotEmpty) {
      return '';
    }
  }
  return 'tapped $budget of $total affordance(s) on $source and none of them '
      'reached $target';
}
"""


def _push_case(screen: Screen, action: Action, destination: str) -> str:
    """The destination renders when the app's own navigator is driven to it."""
    return f"""
  testWidgets('{action.name}: {screen.id} -> {action.target} renders',
      (tester) async {{
    // Called without `await` on purpose. A Flutter entry point is idiomatically
    // declared either `void main()` or `Future<void> main()` — both are
    // ordinary, and awaiting the first is a compile error
    // (`use_of_void_result`), which fails the whole build rather than the test.
    // The settle below gives an async main time to finish initialising before
    // anything touches the widget tree.
    app.main();
    await _settle(tester);

    final destination = find.text({_dart_literal(destination)});
    // `_open` rather than a bare push, because the home screen is
    // conventionally filed at '/' and pushing '/<id>' for it lands on whatever
    // the app returns for an unknown route.
    await _open(tester, '/{action.target}', destination);

    expect(
      destination,
      findsWidgets,
      reason: 'neither the first route nor a push of /{action.target} showed the '
          '{action.target} screen; it is declared but does not render',
    );
  }});"""


def _tap_case(
    screen: Screen, action: Action, destination: str, source_title: str
) -> str:
    """A user on the source screen can get to the target by tapping."""
    return f"""
  testWidgets('{action.name}: a tap on {screen.id} reaches {action.target}',
      (tester) async {{
    // Called without `await` on purpose. A Flutter entry point is idiomatically
    // declared either `void main()` or `Future<void> main()` — both are
    // ordinary, and awaiting the first is a compile error
    // (`use_of_void_result`), which fails the whole build rather than the test.
    // The settle below gives an async main time to finish initialising before
    // anything touches the widget tree.
    app.main();
    await _settle(tester);

    final failure = await _tapReaches(
      tester,
      '/{screen.id}',
      {_dart_literal(source_title)},
      '/{action.target}',
      find.text({_dart_literal(destination)}),
    );

    expect(
      failure,
      isEmpty,
      reason: "the PRD declares '{action.name}' on {screen.id}, so a user must "
          'be able to reach {action.target} from it: $failure',
    );
  }});"""


def build_navigation_test(prd: PRD, files: dict[str, str]) -> dict[str, str]:
    """Return `integration_test/navigation_test.dart`, or nothing to test."""
    titles = {screen.id: screen.title for screen in prd.screens}
    cases: list[str] = []

    for screen in prd.screens:
        for action in screen.actions:
            if action.kind != "navigate" or not action.target:
                continue
            destination = titles.get(action.target)
            if destination is None:
                # The schema rejects unknown targets, so this is unreachable;
                # skipping beats emitting a test that cannot compile.
                continue
            cases.append(_push_case(screen, action, destination))
            cases.append(_tap_case(screen, action, destination, screen.title))

    if not cases:
        return {}

    package = _package_name(prd, files)
    body = "\n".join(cases)
    helpers = _HELPERS % {"budget": _TAP_BUDGET}
    return {
        # Identical to the one `roundtrip.py` writes, and emitted here too so a
        # PRD with navigation but no data model still ships a runnable driver.
        "test_driver/integration_test.dart": (
            "// Generated. Entry point for `flutter drive`.\n"
            "import 'package:integration_test/integration_test_driver.dart';\n"
            "\n"
            "Future<void> main() => integrationDriver();\n"
        ),
        "integration_test/navigation_test.dart": f"""// Generated. Boots the app through its own main() and, for every screen the
// PRD declares as a navigate target, checks two things the routes table alone
// never proved: that the destination renders when navigated to, and that some
// affordance on the source screen actually gets a user there.
import 'package:flutter/cupertino.dart' as cupertino;
import 'package:flutter/material.dart' as material;
import 'package:flutter/widgets.dart' as widgets;
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

import 'package:{package}/main.dart' as app;
{helpers}
void main() {{
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();
{body}
}}
"""
    }
