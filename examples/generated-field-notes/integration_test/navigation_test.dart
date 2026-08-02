// Generated. Boots the app through its own main() and, for every screen the
// PRD declares as a navigate target, checks two things the routes table alone
// never proved: that the destination renders when navigated to, and that some
// affordance on the source screen actually gets a user there.
import 'package:flutter/cupertino.dart' as cupertino;
import 'package:flutter/material.dart' as material;
import 'package:flutter/widgets.dart' as widgets;
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

import 'package:field_notes/main.dart' as app;

/// How many affordances a tap case tries before giving up. A list screen backed
/// by an emulator can render dozens of rows, and each attempt costs a push and
/// a settle; the failure message reports the budget so a genuine overrun cannot
/// be mistaken for "nothing here reaches the target".
const int _tapBudget = 40;

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

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('openCapture: observations -> capture renders',
      (tester) async {
    // Called without `await` on purpose. A Flutter entry point is idiomatically
    // declared either `void main()` or `Future<void> main()` — both are
    // ordinary, and awaiting the first is a compile error
    // (`use_of_void_result`), which fails the whole build rather than the test.
    // The settle below gives an async main time to finish initialising before
    // anything touches the widget tree.
    app.main();
    await _settle(tester);

    final destination = find.text('New observation');
    // `_open` rather than a bare push, because the home screen is
    // conventionally filed at '/' and pushing '/<id>' for it lands on whatever
    // the app returns for an unknown route.
    await _open(tester, '/capture', destination);

    expect(
      destination,
      findsWidgets,
      reason: 'neither the first route nor a push of /capture showed the '
          'capture screen; it is declared but does not render',
    );
  });

  testWidgets('openCapture: a tap on observations reaches capture',
      (tester) async {
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
      '/observations',
      'Observations',
      '/capture',
      find.text('New observation'),
    );

    expect(
      failure,
      isEmpty,
      reason: "the PRD declares 'openCapture' on observations, so a user must "
          'be able to reach capture from it: $failure',
    );
  });
}
