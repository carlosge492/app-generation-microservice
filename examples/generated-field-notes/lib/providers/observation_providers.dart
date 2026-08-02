import 'package:cloud_firestore/cloud_firestore.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/observation.dart';
import 'auth_provider.dart';
import 'firebase_providers.dart';

const String _collection = 'observations';

/// Live list for the signed-in user, ordered by recordedAt descending.
final observationsStreamProvider =
    StreamProvider<List<Observation>>((ref) async* {
  final uid = ref.watch(currentUidProvider);
  if (uid == null) {
    yield const <Observation>[];
    return;
  }

  final db = ref.watch(firestoreProvider);
  final query = db
      .collection(_collection)
      .where('ownerId', isEqualTo: uid)
      .orderBy('recordedAt', descending: true);

  yield* query.snapshots().map(
        (snapshot) => snapshot.docs
            .map((doc) => Observation.fromMap(doc.id, doc.data()))
            .toList(growable: false),
      );
});

/// Total specimen count across all loaded observations.
final specimenTotalProvider = Provider<int>((ref) {
  final items = ref.watch(observationsStreamProvider).valueOrNull;
  if (items == null || items.isEmpty) {
    return 0;
  }
  var total = 0;
  for (final item in items) {
    total += item.count;
  }
  return total;
});

String _friendlyWriteMessage(Object error) {
  if (error is FirebaseException) {
    switch (error.code) {
      case 'permission-denied':
        return 'You do not have permission to do that.';
      case 'unavailable':
        return 'Service unavailable. The change will sync when you are online.';
      case 'not-found':
        return 'That observation no longer exists.';
      default:
        return error.message ?? 'The write failed. Please try again.';
    }
  }
  return 'Something went wrong. Please try again.';
}

class ObservationController extends StateNotifier<AsyncValue<void>> {
  ObservationController(this._db, this._uid)
      : super(const AsyncValue<void>.data(null));

  final FirebaseFirestore _db;
  final String? _uid;

  CollectionReference<Map<String, dynamic>> get _ref =>
      _db.collection(_collection);

  /// Validates the draft, writes a new document, returns true on success.
  Future<bool> create(ObservationDraft draft) async {
    final uid = _uid;
    if (uid == null) {
      state = AsyncValue<void>.error(
        'You must be signed in to save an observation.',
        StackTrace.current,
      );
      return false;
    }

    final validationError = draft.validate();
    if (validationError != null) {
      state = AsyncValue<void>.error(validationError, StackTrace.current);
      return false;
    }

    state = const AsyncValue<void>.loading();
    try {
      final observation = draft.toObservation(ownerId: uid);
      await _ref.add(observation.toMap(forCreate: true));
      if (mounted) {
        state = const AsyncValue<void>.data(null);
      }
      return true;
    } catch (e, st) {
      if (mounted) {
        state = AsyncValue<void>.error(_friendlyWriteMessage(e), st);
      }
      return false;
    }
  }

  /// Flips the `verified` flag on an existing document.
  Future<void> toggleVerified(Observation observation) async {
    if (observation.id.isEmpty) {
      return;
    }
    state = const AsyncValue<void>.loading();
    try {
      await _ref.doc(observation.id).update(<String, dynamic>{
        'verified': !observation.verified,
      });
      if (mounted) {
        state = const AsyncValue<void>.data(null);
      }
    } catch (e, st) {
      if (mounted) {
        state = AsyncValue<void>.error(_friendlyWriteMessage(e), st);
      }
    }
  }

  Future<void> delete(String id) async {
    if (id.isEmpty) {
      return;
    }
    state = const AsyncValue<void>.loading();
    try {
      await _ref.doc(id).delete();
      if (mounted) {
        state = const AsyncValue<void>.data(null);
      }
    } catch (e, st) {
      if (mounted) {
        state = AsyncValue<void>.error(_friendlyWriteMessage(e), st);
      }
    }
  }
}

final observationControllerProvider =
    StateNotifierProvider<ObservationController, AsyncValue<void>>((ref) {
  return ObservationController(
    ref.watch(firestoreProvider),
    ref.watch(currentUidProvider),
  );
});
