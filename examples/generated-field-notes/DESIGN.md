# Field Notes — Frozen Design Specification

> **STATUS: FROZEN.** This document is the single source of truth for all downstream
> agents (GenUI, Logic, Integration, CI). No downstream agent may edit, extend, or
> reinterpret this file. If something is ambiguous, choose the simplest option that
> compiles and matches the signatures declared here verbatim.

---

## 1. Project Identity

| Item | Value |
|---|---|
| App display name | Field Notes |
| Dart package name | `field_notes` |
| Android applicationId / iOS bundle id | `com.example.fieldnotes` |
| Description | A small field-research capture app: log observations offline, sync to Firestore. |
| Theme | Material 3 (`useMaterial3: true`), seed color `Colors.teal` |
| State management | Riverpod (`flutter_riverpod`) — **all** state, no `setState` outside of local `TextEditingController` lifecycles |
| Backend | Firebase (`firebase_core`, `cloud_firestore`, `firebase_auth`) |
| Auth | Required (email + password, anonymous fallback not used) |
| Offline | Firestore local persistence enabled at bootstrap; no custom cache layer |

### Hard constraints (CI will fail otherwise)
1. Standard Material / Cupertino widgets only. No third-party UI packages.
2. No `CustomPainter`, no `CustomPaint`, no shaders, no `AnimationController`,
   no `Tween`, no implicit-animation widgets beyond `AnimatedSwitcher`-free code.
   Page transitions are the default `MaterialPageRoute` ones.
3. No code generation (no `freezed`, no `json_serializable`, no `build_runner`).
   Models are hand-written with `fromFirestore` / `toMap`.
4. Null-safe Dart, `environment.sdk: '>=3.4.0 <4.0.0'`.
5. Every file must compile with zero analyzer errors under the default
   `flutter_lints` ruleset. Warnings are tolerated; errors are not.

---

## 2. File Layout (authoritative)

```
lib/
  main.dart                        # Integration agent
  firebase_options.dart            # Integration agent (placeholder values OK)
  models/
    observation.dart               # Logic agent
  providers/
    firebase_providers.dart        # Logic agent
    auth_provider.dart             # Logic agent
    observation_providers.dart     # Logic agent
  ui/
    app.dart                       # GenUI agent (MaterialApp + routing shell)
    auth_gate.dart                 # GenUI agent
    sign_in_screen.dart            # GenUI agent
    observations_screen.dart       # GenUI agent
    capture_screen.dart            # GenUI agent
    settings_screen.dart           # GenUI agent
    widgets/
      observation_tile.dart        # GenUI agent
      async_value_view.dart        # GenUI agent
```

No other files are created. No `test/` requirements beyond the default
`widget_test.dart` being deleted or left compiling.

---

## 3. Ownership Boundary

### 3.1 Logic subagent owns `lib/models/` and `lib/providers/`
- Declares every model class and every Riverpod provider.
- Owns *all* Firestore reads/writes, all `FirebaseAuth` calls, all validation
  helpers, all error mapping to user-facing strings.
- **Must not** import anything from `package:flutter/material.dart` except
  `package:flutter/foundation.dart` types (`immutable`, `debugPrint`). No widgets,
  no `BuildContext`, no `ScaffoldMessenger`, no navigation.

### 3.2 GenUI subagent owns `lib/ui/`
- Builds all widgets, layout, form fields, snackbars, dialogs, navigation.
- Reads state **only** through the providers named in §5, using `ref.watch`.
- Triggers behaviour **only** through the controller methods named in §5, using
  `ref.read(...).method(...)`.
- **Must not** import `cloud_firestore`, `firebase_auth`, or `firebase_core`.
  **Must not** construct a `Query`, `DocumentReference`, or `Timestamp`.
  If the UI needs a date it uses Dart `DateTime` only.

### 3.3 Integration subagent owns `lib/main.dart` and `lib/firebase_options.dart`
- `main()` is `async`, calls `WidgetsFlutterBinding.ensureInitialized()`,
  `await Firebase.initializeApp(options: DefaultFirebaseOptions.currentPlatform)`,
  then `runApp(const ProviderScope(child: FieldNotesApp()))`.
- Enables Firestore persistence via
  `FirebaseFirestore.instance.settings = const Settings(persistenceEnabled: true);`
  wrapped in a `try/catch` (web throws on some platforms).

### 3.4 Shared contract
The *only* coupling surface is: model class names + field names in §4, and the
provider/controller signatures in §5. Both agents treat these as literal API.

---

## 4. Data Model

### 4.1 `Observation` — Firestore collection `observations`

Flat top-level collection. Every document carries an `ownerId` so security rules
and queries can scope by user.

| Dart field | Type | Firestore key | Firestore type | Required | Notes |
|---|---|---|---|---|---|
| `id` | `String` | *(doc id)* | — | yes | Empty string for unsaved drafts. |
| `title` | `String` | `title` | string | yes | Label "Title". Non-empty, trimmed, max 120 chars. |
| `notes` | `String` | `notes` | string | no | Label "Notes". Defaults to `''`. Multiline. |
| `count` | `int` | `count` | number | yes | Label "Specimen count". `>= 0`. Defaults to `0`. |
| `verified` | `bool` | `verified` | bool | yes | Label "Verified". Defaults to `false`. |
| `recordedAt` | `DateTime` | `recordedAt` | Timestamp | yes | Label "Recorded at". Defaults to `DateTime.now()` at form open. |
| `ownerId` | `String` | `ownerId` | string | yes | `FirebaseAuth.instance.currentUser!.uid`. |
| `createdAt` | `DateTime?` | `createdAt` | Timestamp | server | Written with `FieldValue.serverTimestamp()`; may be `null` locally until the write round-trips. |

### 4.2 Exact class contract (Logic agent implements verbatim)

```dart
@immutable
class Observation {
  const Observation({
    required this.id,
    required this.title,
    required this.notes,
    required this.count,
    required this.verified,
    required this.recordedAt,
    required this.ownerId,
    this.createdAt,
  });

  final String id;
  final String title;
  final String notes;
  final int count;
  final bool verified;
  final DateTime recordedAt;
  final String ownerId;
  final DateTime? createdAt;

  /// Reads a Firestore snapshot defensively: every field falls back to the
  /// default in \u00a74.1 if missing or of the wrong runtime type.
  factory Observation.fromMap(String id, Map<String, dynamic> data);

  factory Observation.fromDoc(DocumentSnapshot<Map<String, dynamic>> doc);

  /// Excludes [id]. Includes `createdAt: FieldValue.serverTimestamp()` only when
  /// [forCreate] is true.
  Map<String, dynamic> toMap({bool forCreate = false});

  Observation copyWith({
    String? id,
    String? title,
    String? notes,
    int? count,
    bool? verified,
    DateTime? recordedAt,
    String? ownerId,
    DateTime? createdAt,
  });
}
```

Also in `lib/models/observation.dart`:

```dart
/// Mutable draft used by the capture form. UI-safe: no Firestore types.
class ObservationDraft {
  ObservationDraft({
    this.title = '',
    this.notes = '',
    this.count = 0,
    this.verified = false,
    DateTime? recordedAt,
  }) : recordedAt = recordedAt ?? DateTime.now();

  String title;
  String notes;
  int count;
  bool verified;
  DateTime recordedAt;

  /// Returns null when valid, otherwise a user-facing message.
  String? validate();
}
```

---

## 5. Provider / Controller Contract

All providers live in `lib/providers/`. Names are literal and final.

### 5.1 `firebase_providers.dart`
```dart
final firestoreProvider = Provider<FirebaseFirestore>((ref) => FirebaseFirestore.instance);
final firebaseAuthProvider = Provider<FirebaseAuth>((ref) => FirebaseAuth.instance);
```

### 5.2 `auth_provider.dart`
```dart
/// Streams the raw auth state. `null` == signed out.
final authStateProvider = StreamProvider<User?>((ref) => ...);

/// Convenience: current uid or null. Derived from [authStateProvider].
final currentUidProvider = Provider<String?>((ref) => ...);

/// Convenience: current email or null, for the Settings screen.
final currentEmailProvider = Provider<String?>((ref) => ...);

class AuthController extends StateNotifier<AsyncValue<void>> {
  AuthController(this._auth) : super(const AsyncValue.data(null));
  Future<void> signIn({required String email, required String password});
  Future<void> signUp({required String email, required String password});
  Future<void> signOut();
}

final authControllerProvider =
    StateNotifierProvider<AuthController, AsyncValue<void>>((ref) => ...);
```
All three methods catch `FirebaseAuthException` and expose a friendly message via
`AsyncValue.error(String message, StackTrace)` — the UI renders `error.toString()`
directly, so the error object must be a plain `String`.

### 5.3 `observation_providers.dart`
```dart
/// Live list for the signed-in user, ordered by recordedAt descending.
/// Query: collection('observations')
///          .where('ownerId', isEqualTo: uid)
///          .orderBy('recordedAt', descending: true)
/// Emits `const <Observation>[]` immediately when uid is null.
final observationsStreamProvider = StreamProvider<List<Observation>>((ref) => ...);

/// Total specimen count across all loaded observations. Derived, never awaited.
final specimenTotalProvider = Provider<int>((ref) => ...);

class ObservationController extends StateNotifier<AsyncValue<void>> {
  ObservationController(this._db, this._uid) : super(const AsyncValue.data(null));

  /// Validates the draft, writes a new document, returns true on success.
  /// On failure sets state to AsyncValue.error(<String message>, st) and returns false.
  Future<bool> create(ObservationDraft draft);

  /// Flips the `verified` flag on an existing document.
  Future<void> toggleVerified(Observation observation);

  Future<void> delete(String id);
}

final observationControllerProvider =
    StateNotifierProvider<ObservationController, AsyncValue<void>>((ref) => ...);
```

**Rule for GenUI:** the only way to persist anything is
`ref.read(observationControllerProvider.notifier).create(draft)`. The only way to
read the list is `ref.watch(observationsStreamProvider)`.

---

## 6. Screens

Navigation is imperative `Navigator.push(MaterialPageRoute(...))` — no named
routes, no router package. Route ids below are logical only.

### 6.0 `AuthGate` (`lib/ui/auth_gate.dart`)
Watches `authStateProvider`.
- `loading` → `Scaffold` with centered `CircularProgressIndicator`.
- `error` → `Scaffold` with centered error text.
- data `null` → `SignInScreen`.
- data non-null → `ObservationsScreen`.

### 6.1 `SignInScreen`
- `AppBar(title: Text('Field Notes'))`.
- `Form` with `TextFormField` email (validator: contains `@`) and
  `TextFormField` password (obscured, min 6 chars).
- `FilledButton` "Sign in" → `AuthController.signIn`.
- `TextButton` "Create account" → `AuthController.signUp`.
- Watches `authControllerProvider`; shows `CircularProgressIndicator` in place of
  the buttons while loading and a `SnackBar` on error.

### 6.2 `observations` — `ObservationsScreen` (kind: list, model: Observation)
- `AppBar(title: Text('Observations'))` with one `IconButton(Icons.settings)` →
  pushes `SettingsScreen`.
- Body: `ref.watch(observationsStreamProvider).when(...)`
  - loading → centered `CircularProgressIndicator`
  - error → centered `Text` with the error and a `TextButton` "Retry" that calls
    `ref.invalidate(observationsStreamProvider)`
  - data empty → centered `Column` with `Icon(Icons.eco_outlined)` and
    "No observations yet. Tap + to log one."
  - data non-empty → `ListView.separated` of `ObservationTile`, with a
    non-scrolling header `ListTile` showing "Total specimens: {specimenTotalProvider}".
- `ObservationTile`: `ListTile` with `title` = observation title,
  `subtitle` = `'{count} specimens \u00b7 {formatted recordedAt}'` plus notes on a
  second line when non-empty, `leading` = `CircleAvatar` with the count,
  `trailing` = `Checkbox` bound to `verified` calling
  `toggleVerified`. `onLongPress` shows an `AlertDialog` confirming delete.
- Date formatting is done in the UI with a tiny local helper
  (`'${d.year}-${d.month.toString().padLeft(2,'0')}-${d.day.toString().padLeft(2,'0')}'`).
  **No `intl` dependency.**
- Action `openCapture`: `FloatingActionButton(child: Icon(Icons.add))` pushes
  `CaptureScreen`.

### 6.3 `capture` — `CaptureScreen` (kind: form, model: Observation)
- `AppBar(title: Text('New observation'))`.
- `StatefulWidget` + `ConsumerStatefulWidget`; holds a local `ObservationDraft`,
  a `GlobalKey<FormState>`, and `TextEditingController`s. Local controllers are
  the *only* permitted non-Riverpod state.
- Fields, in order:
  1. `title` → `TextFormField`, `labelText: 'Title'`, required validator.
  2. `notes` → `TextFormField`, `labelText: 'Notes'`, `maxLines: 4`, optional.
  3. `count` → `TextFormField`, `labelText: 'Specimen count'`,
     `keyboardType: TextInputType.number`, validator rejects non-integers and negatives.
  4. `verified` → `SwitchListTile`, `title: Text('Verified')`.
  5. `recordedAt` → `ListTile` with `title: Text('Recorded at')`,
     `subtitle` = formatted date, `trailing: Icon(Icons.calendar_today)`,
     `onTap` → `showDatePicker` (range 2000-01-01 .. now + 1 day).
- Action `saveObservation`: `FilledButton` "Save" in a bottom `SafeArea`.
  Validates the `Form`, copies controller text into the draft, calls
  `create(draft)`. On `true` → `Navigator.pop(context)` then a `SnackBar`
  "Observation saved" shown by the *list* screen is **not** required; the capture
  screen shows the SnackBar before popping. On `false` → `SnackBar` with the
  controller's error message, stay on screen.
- Button is disabled (`onPressed: null`) while
  `ref.watch(observationControllerProvider).isLoading`.

### 6.4 `settings` — `SettingsScreen` (kind: settings)
- `AppBar(title: Text('Settings'))`.
- `ListView` of:
  - `ListTile` "Signed in as" / subtitle `currentEmailProvider ?? 'Unknown'`,
    `leading: Icon(Icons.person_outline)`.
  - `ListTile` "Observations stored" / subtitle = list length from
    `observationsStreamProvider.valueOrNull?.length ?? 0`.
  - `Divider`.
  - Action `signOut`: `ListTile` "Sign out", `leading: Icon(Icons.logout)`,
    `textColor` = `Theme.of(context).colorScheme.error`. Shows an `AlertDialog`
    confirm, then `ref.read(authControllerProvider.notifier).signOut()` and pops
    back to the root with `Navigator.of(context).popUntil((r) => r.isFirst)`.
    `AuthGate` then rebuilds into `SignInScreen`.

---

## 7. Firestore Rules (documentation only, not shipped as code)

```
match /databases/{db}/documents {
  match /observations/{id} {
    allow read, delete: if request.auth != null && resource.data.ownerId == request.auth.uid;
    allow create: if request.auth != null && request.resource.data.ownerId == request.auth.uid;
    allow update: if request.auth != null && resource.data.ownerId == request.auth.uid;
  }
}
```

---

## 8. Error & Empty-State Policy

| Situation | Behaviour |
|---|---|
| Firestore stream error | Inline centered message + Retry (`ref.invalidate`). |
| Write failure | `SnackBar` with the controller's `String` error; form retains input. |
| Auth failure | `SnackBar`; fields retain input. |
| Offline | No special UI. Firestore persistence queues the write; the optimistic local snapshot makes the row appear immediately. |
| Unauthenticated but on a pushed screen | Not possible: `AuthGate` is the only entry point and pushed screens are popped on sign-out. |

---

## 9. Definition of Done

1. `flutter pub get` and `flutter analyze` succeed with zero errors.
2. `flutter build apk --debug` (or `flutter build web`) succeeds with placeholder
   `firebase_options.dart`.
3. Every provider in §5 exists with the exact declared name and signature.
4. No file in `lib/ui/` imports `cloud_firestore`, `firebase_auth`, or
   `firebase_core`. No file in `lib/providers/` imports `flutter/material.dart`.
5. Note: `x402_payment_verified` is `false` in the PRD — no payment, billing,
   paywall, or entitlement logic is implemented anywhere in this app.
