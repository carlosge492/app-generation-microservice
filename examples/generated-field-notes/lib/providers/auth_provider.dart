import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'firebase_providers.dart';

/// Streams the raw auth state. `null` == signed out.
final authStateProvider = StreamProvider<User?>((ref) {
  return ref.watch(firebaseAuthProvider).authStateChanges();
});

/// Convenience: current uid or null.
final currentUidProvider = Provider<String?>((ref) {
  return ref.watch(authStateProvider).valueOrNull?.uid;
});

/// Convenience: current email or null, for the Settings screen.
final currentEmailProvider = Provider<String?>((ref) {
  return ref.watch(authStateProvider).valueOrNull?.email;
});

String _friendlyAuthMessage(FirebaseAuthException e) {
  switch (e.code) {
    case 'invalid-email':
      return 'That email address is not valid.';
    case 'user-disabled':
      return 'This account has been disabled.';
    case 'user-not-found':
      return 'No account found for that email.';
    case 'wrong-password':
    case 'invalid-credential':
      return 'Incorrect email or password.';
    case 'email-already-in-use':
      return 'An account already exists for that email.';
    case 'weak-password':
      return 'Password must be at least 6 characters.';
    case 'operation-not-allowed':
      return 'Email and password sign-in is not enabled.';
    case 'too-many-requests':
      return 'Too many attempts. Please try again later.';
    case 'network-request-failed':
      return 'Network unavailable. Check your connection.';
    default:
      return e.message ?? 'Authentication failed. Please try again.';
  }
}

class AuthController extends StateNotifier<AsyncValue<void>> {
  AuthController(this._auth) : super(const AsyncValue<void>.data(null));

  final FirebaseAuth _auth;

  Future<void> signIn({
    required String email,
    required String password,
  }) async {
    state = const AsyncValue<void>.loading();
    try {
      await _auth.signInWithEmailAndPassword(
        email: email.trim(),
        password: password,
      );
      if (!mounted) {
        return;
      }
      state = const AsyncValue<void>.data(null);
    } on FirebaseAuthException catch (e, st) {
      if (!mounted) {
        return;
      }
      state = AsyncValue<void>.error(_friendlyAuthMessage(e), st);
    } catch (e, st) {
      if (!mounted) {
        return;
      }
      state = AsyncValue<void>.error('Could not sign in. Please try again.', st);
    }
  }

  Future<void> signUp({
    required String email,
    required String password,
  }) async {
    state = const AsyncValue<void>.loading();
    try {
      await _auth.createUserWithEmailAndPassword(
        email: email.trim(),
        password: password,
      );
      if (!mounted) {
        return;
      }
      state = const AsyncValue<void>.data(null);
    } on FirebaseAuthException catch (e, st) {
      if (!mounted) {
        return;
      }
      state = AsyncValue<void>.error(_friendlyAuthMessage(e), st);
    } catch (e, st) {
      if (!mounted) {
        return;
      }
      state = AsyncValue<void>.error(
        'Could not create the account. Please try again.',
        st,
      );
    }
  }

  Future<void> signOut() async {
    state = const AsyncValue<void>.loading();
    try {
      await _auth.signOut();
      if (!mounted) {
        return;
      }
      state = const AsyncValue<void>.data(null);
    } on FirebaseAuthException catch (e, st) {
      if (!mounted) {
        return;
      }
      state = AsyncValue<void>.error(_friendlyAuthMessage(e), st);
    } catch (e, st) {
      if (!mounted) {
        return;
      }
      state =
          AsyncValue<void>.error('Could not sign out. Please try again.', st);
    }
  }
}

final authControllerProvider =
    StateNotifierProvider<AuthController, AsyncValue<void>>((ref) {
  return AuthController(ref.watch(firebaseAuthProvider));
});
