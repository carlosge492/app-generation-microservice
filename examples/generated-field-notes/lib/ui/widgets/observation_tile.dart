import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/observation.dart';
import '../../providers/observation_providers.dart';

/// Tiny local date formatter — no `intl` dependency.
String formatObservationDate(DateTime d) {
  return '${d.year}-${d.month.toString().padLeft(2, '0')}-'
      '${d.day.toString().padLeft(2, '0')}';
}

class ObservationTile extends ConsumerWidget {
  const ObservationTile({super.key, required this.observation});

  final Observation observation;

  Future<void> _confirmDelete(BuildContext context, WidgetRef ref) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) {
        return AlertDialog(
          title: const Text('Delete observation'),
          content: Text('Delete "${observation.title}"? This cannot be undone.'),
          actions: <Widget>[
            TextButton(
              onPressed: () => Navigator.of(dialogContext).pop(false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.of(dialogContext).pop(true),
              child: const Text('Delete'),
            ),
          ],
        );
      },
    );

    if (confirmed == true) {
      await ref
          .read(observationControllerProvider.notifier)
          .delete(observation.id);
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final buffer = StringBuffer(
      '${observation.count} specimens \u00b7 '
      '${formatObservationDate(observation.recordedAt)}',
    );
    if (observation.notes.isNotEmpty) {
      buffer.write('\n${observation.notes}');
    }

    return ListTile(
      leading: CircleAvatar(child: Text('${observation.count}')),
      title: Text(observation.title),
      subtitle: Text(buffer.toString()),
      isThreeLine: observation.notes.isNotEmpty,
      trailing: Checkbox(
        value: observation.verified,
        onChanged: (_) {
          ref
              .read(observationControllerProvider.notifier)
              .toggleVerified(observation);
        },
      ),
      onLongPress: () => _confirmDelete(context, ref),
    );
  }
}
