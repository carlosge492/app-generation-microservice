import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/observation.dart';
import '../providers/observation_providers.dart';

String _formatDate(DateTime d) {
  return '${d.year}-${d.month.toString().padLeft(2, '0')}-'
      '${d.day.toString().padLeft(2, '0')}';
}

class CaptureScreen extends ConsumerStatefulWidget {
  const CaptureScreen({super.key});

  @override
  ConsumerState<CaptureScreen> createState() => _CaptureScreenState();
}

class _CaptureScreenState extends ConsumerState<CaptureScreen> {
  final GlobalKey<FormState> _formKey = GlobalKey<FormState>();
  final TextEditingController _titleController = TextEditingController();
  final TextEditingController _notesController = TextEditingController();
  final TextEditingController _countController =
      TextEditingController(text: '0');

  final ValueNotifier<bool> _verified = ValueNotifier<bool>(false);
  late final ValueNotifier<DateTime> _recordedAt =
      ValueNotifier<DateTime>(DateTime.now());

  @override
  void dispose() {
    _titleController.dispose();
    _notesController.dispose();
    _countController.dispose();
    _verified.dispose();
    _recordedAt.dispose();
    super.dispose();
  }

  Future<void> _pickDate() async {
    final now = DateTime.now();
    final picked = await showDatePicker(
      context: context,
      initialDate: _recordedAt.value,
      firstDate: DateTime(2000, 1, 1),
      lastDate: now.add(const Duration(days: 1)),
    );
    if (picked != null) {
      _recordedAt.value = DateTime(
        picked.year,
        picked.month,
        picked.day,
        _recordedAt.value.hour,
        _recordedAt.value.minute,
      );
    }
  }

  Future<void> _save() async {
    final form = _formKey.currentState;
    if (form == null || !form.validate()) {
      return;
    }

    final draft = ObservationDraft(
      title: _titleController.text.trim(),
      notes: _notesController.text.trim(),
      count: int.tryParse(_countController.text.trim()) ?? 0,
      verified: _verified.value,
      recordedAt: _recordedAt.value,
    );

    final ok =
        await ref.read(observationControllerProvider.notifier).create(draft);
    if (!mounted) {
      return;
    }

    if (ok) {
      ScaffoldMessenger.of(context)
        ..hideCurrentSnackBar()
        ..showSnackBar(const SnackBar(content: Text('Observation saved')));
      Navigator.of(context).pop();
    } else {
      final error = ref.read(observationControllerProvider).error;
      ScaffoldMessenger.of(context)
        ..hideCurrentSnackBar()
        ..showSnackBar(
          SnackBar(
            content: Text(
              error?.toString() ?? 'Could not save the observation.',
            ),
          ),
        );
    }
  }

  @override
  Widget build(BuildContext context) {
    final saving = ref.watch(observationControllerProvider).isLoading;

    return Scaffold(
      appBar: AppBar(title: const Text('New observation')),
      body: Form(
        key: _formKey,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: <Widget>[
            TextFormField(
              controller: _titleController,
              textInputAction: TextInputAction.next,
              maxLength: 120,
              decoration: const InputDecoration(
                labelText: 'Title',
                border: OutlineInputBorder(),
              ),
              validator: (value) {
                final text = (value ?? '').trim();
                if (text.isEmpty) {
                  return 'Title is required';
                }
                if (text.length > 120) {
                  return 'Title must be 120 characters or fewer';
                }
                return null;
              },
            ),
            const SizedBox(height: 12),
            TextFormField(
              controller: _notesController,
              maxLines: 4,
              decoration: const InputDecoration(
                labelText: 'Notes',
                alignLabelWithHint: true,
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 16),
            TextFormField(
              controller: _countController,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(
                labelText: 'Specimen count',
                border: OutlineInputBorder(),
              ),
              validator: (value) {
                final text = (value ?? '').trim();
                if (text.isEmpty) {
                  return 'Specimen count is required';
                }
                final parsed = int.tryParse(text);
                if (parsed == null) {
                  return 'Enter a whole number';
                }
                if (parsed < 0) {
                  return 'Count cannot be negative';
                }
                return null;
              },
            ),
            const SizedBox(height: 8),
            ValueListenableBuilder<bool>(
              valueListenable: _verified,
              builder: (context, value, _) {
                return SwitchListTile(
                  title: const Text('Verified'),
                  value: value,
                  onChanged: (next) => _verified.value = next,
                );
              },
            ),
            ValueListenableBuilder<DateTime>(
              valueListenable: _recordedAt,
              builder: (context, value, _) {
                return ListTile(
                  title: const Text('Recorded at'),
                  subtitle: Text(_formatDate(value)),
                  trailing: const Icon(Icons.calendar_today),
                  onTap: _pickDate,
                );
              },
            ),
            const SizedBox(height: 24),
          ],
        ),
      ),
      bottomNavigationBar: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
          child: FilledButton(
            onPressed: saving ? null : _save,
            child: saving
                ? const SizedBox(
                    height: 20,
                    width: 20,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Text('Save'),
          ),
        ),
      ),
    );
  }
}
