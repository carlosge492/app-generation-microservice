import 'package:cloud_firestore/cloud_firestore.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'ui/app.dart';

const String _projectId = String.fromEnvironment('FIREBASE_PROJECT_ID');
const String _apiKey = String.fromEnvironment('FIREBASE_API_KEY');
const String _appId = String.fromEnvironment('FIREBASE_APP_ID');
const String _messagingSenderId =
    String.fromEnvironment('FIREBASE_MESSAGING_SENDER_ID');

/// Returns options built from `--dart-define` values, or `null` so that a
/// platform carrying a native config file (google-services.json /
/// GoogleService-Info.plist) can initialise itself.
FirebaseOptions? _firebaseOptionsFromEnvironment() {
  if (_projectId.isEmpty) {
    return null;
  }
  return const FirebaseOptions(
    apiKey: _apiKey,
    appId: _appId,
    messagingSenderId: _messagingSenderId,
    projectId: _projectId,
    storageBucket: '$_projectId.appspot.com',
  );
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final options = _firebaseOptionsFromEnvironment();
  try {
    if (options != null) {
      await Firebase.initializeApp(options: options);
    } else {
      await Firebase.initializeApp();
    }
  } catch (e) {
    debugPrint('Firebase initialisation failed: $e');
  }

  try {
    FirebaseFirestore.instance.settings =
        const Settings(persistenceEnabled: true);
  } catch (e) {
    debugPrint('Firestore persistence unavailable: $e');
  }

  runApp(const ProviderScope(child: FieldNotesApp()));
}
