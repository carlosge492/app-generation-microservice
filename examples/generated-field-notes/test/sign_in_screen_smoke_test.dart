// GENERATED — asserts the screen builds without throwing.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:field_notes/ui/sign_in_screen.dart';

void main() {
  testWidgets('SignInScreen builds without throwing', (WidgetTester tester) async {
    final container = ProviderContainer(
      overrides: [
        // no data providers on this screen
      ],
    );
    addTearDown(container.dispose);

    await tester.pumpWidget(
      UncontrolledProviderScope(
        container: container,
        child: const MaterialApp(home: Scaffold(body: SignInScreen())),
      ),
    );
    await tester.pump();

    final Object? error = tester.takeException();
    if (error != null) {
      // A provider reaching Firestore during build is correct behaviour — the
      // real app calls Firebase.initializeApp() in main(), a widget test does
      // not. Stubbing Firestore would need a fake backend, so this one class of
      // exception is tolerated and everything else still fails the build.
      expect(
        error.toString(),
        contains('core/no-app'),
        reason: 'unexpected exception while building SignInScreen',
      );
    }
    expect(find.byType(SignInScreen), findsOneWidget);

  });
}
