import 'package:flutter/material.dart';

import 'auth_gate.dart';

/// Root application shell. Composed by `main.dart` inside a `ProviderScope`.
class FieldNotesApp extends StatelessWidget {
  const FieldNotesApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Field Notes',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.teal),
      ),
      home: const AuthGate(),
    );
  }
}
