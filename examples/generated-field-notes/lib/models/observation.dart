import 'package:cloud_firestore/cloud_firestore.dart';
import 'package:flutter/foundation.dart';

/// Converts whatever Firestore (or a local optimistic snapshot) hands back into
/// a Dart [DateTime]. Firestore has no date type: values written as a
/// [Timestamp] come back as a [Timestamp], and `FieldValue.serverTimestamp()`
/// reads back as `null` until the write round-trips.
DateTime? _readDate(Object? value) {
  if (value is Timestamp) {
    return value.toDate();
  }
  if (value is DateTime) {
    return value;
  }
  if (value is int) {
    return DateTime.fromMillisecondsSinceEpoch(value);
  }
  if (value is String) {
    return DateTime.tryParse(value);
  }
  return null;
}

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

  /// Reads a Firestore map defensively: every field falls back to its default
  /// when missing or of the wrong runtime type.
  factory Observation.fromMap(String id, Map<String, dynamic> data) {
    final Object? rawTitle = data['title'];
    final Object? rawNotes = data['notes'];
    final Object? rawCount = data['count'];
    final Object? rawVerified = data['verified'];
    final Object? rawOwnerId = data['ownerId'];

    return Observation(
      id: id,
      title: rawTitle is String ? rawTitle : '',
      notes: rawNotes is String ? rawNotes : '',
      count: rawCount is num ? rawCount.toInt() : 0,
      verified: rawVerified is bool ? rawVerified : false,
      recordedAt: _readDate(data['recordedAt']) ?? DateTime.now(),
      ownerId: rawOwnerId is String ? rawOwnerId : '',
      createdAt: _readDate(data['createdAt']),
    );
  }

  factory Observation.fromDoc(DocumentSnapshot<Map<String, dynamic>> doc) {
    return Observation.fromMap(doc.id, doc.data() ?? <String, dynamic>{});
  }

  final String id;
  final String title;
  final String notes;
  final int count;
  final bool verified;
  final DateTime recordedAt;
  final String ownerId;
  final DateTime? createdAt;

  /// Excludes [id]. Adds a server timestamp for `createdAt` only on create.
  Map<String, dynamic> toMap({bool forCreate = false}) {
    final map = <String, dynamic>{
      'title': title,
      'notes': notes,
      'count': count,
      'verified': verified,
      'recordedAt': Timestamp.fromDate(recordedAt),
      'ownerId': ownerId,
    };
    if (forCreate) {
      map['createdAt'] = FieldValue.serverTimestamp();
    }
    return map;
  }

  Observation copyWith({
    String? id,
    String? title,
    String? notes,
    int? count,
    bool? verified,
    DateTime? recordedAt,
    String? ownerId,
    DateTime? createdAt,
  }) {
    return Observation(
      id: id ?? this.id,
      title: title ?? this.title,
      notes: notes ?? this.notes,
      count: count ?? this.count,
      verified: verified ?? this.verified,
      recordedAt: recordedAt ?? this.recordedAt,
      ownerId: ownerId ?? this.ownerId,
      createdAt: createdAt ?? this.createdAt,
    );
  }

  @override
  bool operator ==(Object other) {
    if (identical(this, other)) {
      return true;
    }
    return other is Observation &&
        other.id == id &&
        other.title == title &&
        other.notes == notes &&
        other.count == count &&
        other.verified == verified &&
        other.recordedAt == recordedAt &&
        other.ownerId == ownerId &&
        other.createdAt == createdAt;
  }

  @override
  int get hashCode => Object.hash(
        id,
        title,
        notes,
        count,
        verified,
        recordedAt,
        ownerId,
        createdAt,
      );
}

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
  String? validate() {
    final trimmedTitle = title.trim();
    if (trimmedTitle.isEmpty) {
      return 'Title is required.';
    }
    if (trimmedTitle.length > 120) {
      return 'Title must be 120 characters or fewer.';
    }
    if (count < 0) {
      return 'Specimen count cannot be negative.';
    }
    return null;
  }

  /// Builds the immutable model this draft describes.
  Observation toObservation({required String ownerId}) {
    return Observation(
      id: '',
      title: title.trim(),
      notes: notes.trim(),
      count: count,
      verified: verified,
      recordedAt: recordedAt,
      ownerId: ownerId,
    );
  }
}
