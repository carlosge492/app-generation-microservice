import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/observation.dart';
import '../providers/observation_providers.dart';
import 'capture_screen.dart';
import 'settings_screen.dart';
import 'widgets/async_value_view.dart';
import 'widgets/observation_tile.dart';

class ObservationsScreen extends ConsumerWidget {
  const ObservationsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final observations = ref.watch(observationsStreamProvider);
    final total = ref.watch(specimenTotalProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Observations'),
        actions: <Widget>[
          IconButton(
            icon: const Icon(Icons.settings),
            tooltip: 'Settings',
            onPressed: () {
              Navigator.of(context).push(
                MaterialPageRoute<void>(
                  builder: (_) => const SettingsScreen(),
                ),
              );
            },
          ),
        ],
      ),
      body: AsyncValueView<List<Observation>>(
        value: observations,
        onRetry: () => ref.invalidate(observationsStreamProvider),
        data: (items) {
          if (items.isEmpty) {
            return const Center(
              child: Padding(
                padding: EdgeInsets.all(24),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: <Widget>[
                    Icon(Icons.eco_outlined, size: 56),
                    SizedBox(height: 12),
                    Text(
                      'No observations yet. Tap + to log one.',
                      textAlign: TextAlign.center,
                    ),
                  ],
                ),
              ),
            );
          }

          return Column(
            children: <Widget>[
              ListTile(
                leading: const Icon(Icons.summarize_outlined),
                title: Text('Total specimens: $total'),
              ),
              const Divider(height: 1),
              Expanded(
                child: ListView.separated(
                  itemCount: items.length,
                  separatorBuilder: (_, __) => const Divider(height: 1),
                  itemBuilder: (context, index) {
                    return ObservationTile(observation: items[index]);
                  },
                ),
              ),
            ],
          );
        },
      ),
      floatingActionButton: FloatingActionButton(
        tooltip: 'New observation',
        onPressed: () {
          Navigator.of(context).push(
            MaterialPageRoute<void>(
              builder: (_) => const CaptureScreen(),
            ),
          );
        },
        child: const Icon(Icons.add),
      ),
    );
  }
}
