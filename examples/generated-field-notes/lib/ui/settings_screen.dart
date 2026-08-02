import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/auth_provider.dart';
import '../providers/observation_providers.dart';

class SettingsScreen extends ConsumerWidget {
  const SettingsScreen({super.key});

  Future<void> _confirmSignOut(BuildContext context, WidgetRef ref) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) {
        return AlertDialog(
          title: const Text('Sign out'),
          content: const Text('Are you sure you want to sign out?'),
          actions: <Widget>[
            TextButton(
              onPressed: () => Navigator.of(dialogContext).pop(false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.of(dialogContext).pop(true),
              child: const Text('Sign out'),
            ),
          ],
        );
      },
    );

    if (confirmed != true) {
      return;
    }

    await ref.read(authControllerProvider.notifier).signOut();
    if (!context.mounted) {
      return;
    }
    Navigator.of(context).popUntil((route) => route.isFirst);
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final email = ref.watch(currentEmailProvider);
    final storedCount =
        ref.watch(observationsStreamProvider).valueOrNull?.length ?? 0;
    final errorColor = Theme.of(context).colorScheme.error;

    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: ListView(
        children: <Widget>[
          ListTile(
            leading: const Icon(Icons.person_outline),
            title: const Text('Signed in as'),
            subtitle: Text(email ?? 'Unknown'),
          ),
          ListTile(
            leading: const Icon(Icons.list_alt_outlined),
            title: const Text('Observations stored'),
            subtitle: Text('$storedCount'),
          ),
          const Divider(),
          ListTile(
            leading: Icon(Icons.logout, color: errorColor),
            title: const Text('Sign out'),
            textColor: errorColor,
            onTap: () => _confirmSignOut(context, ref),
          ),
        ],
      ),
    );
  }
}
