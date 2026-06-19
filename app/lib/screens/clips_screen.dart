import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../api.dart';
import '../controller.dart';
import '../ui_helpers.dart';

class ClipsScreen extends StatelessWidget {
  final AppController controller;
  const ClipsScreen({super.key, required this.controller});

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: controller,
      builder: (context, _) {
        final summary = controller.summary;
        final clips = summary?.clips.reversed.toList() ?? const <ClipInfo>[];
        return RefreshIndicator(
          onRefresh: controller.refresh,
          child: clips.isEmpty
              ? ListView(
                  children: [
                    const SizedBox(height: 120),
                    Center(
                      child: Text(
                        controller.summary == null
                            ? 'Recent clips unavailable.\nPull to refresh.'
                            : 'No clips in ${windowLabel(controller.window)}.',
                        textAlign: TextAlign.center,
                      ),
                    ),
                  ],
                )
              : ListView.builder(
                  padding: const EdgeInsets.fromLTRB(12, 12, 12, 24),
                  itemCount: clips.length + 1,
                  itemBuilder: (context, i) {
                    if (i == 0) {
                      return Padding(
                        padding: const EdgeInsets.only(bottom: 8, left: 4),
                        child: Text(
                          '${clips.length} clips · ${windowLabel(controller.window)}',
                          style: Theme.of(context).textTheme.titleSmall,
                        ),
                      );
                    }
                    return _ClipCard(clip: clips[i - 1]);
                  },
                ),
        );
      },
    );
  }
}

class _ClipCard extends StatelessWidget {
  final ClipInfo clip;
  const _ClipCard({required this.clip});

  @override
  Widget build(BuildContext context) {
    final time = clip.startIso != null
        ? DateFormat('MMM d, HH:mm:ss').format(clip.startIso!.toLocal())
        : '—';
    final objects = clip.objects.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Text('Clip #${clip.id}',
                    style: Theme.of(context)
                        .textTheme
                        .titleMedium
                        ?.copyWith(fontWeight: FontWeight.bold)),
                const Spacer(),
                Text(timeAgo(clip.endIso),
                    style: Theme.of(context).textTheme.bodySmall),
              ],
            ),
            const SizedBox(height: 2),
            Text(time, style: Theme.of(context).textTheme.bodySmall),
            const SizedBox(height: 10),
            if (objects.isEmpty)
              Text('No objects',
                  style: Theme.of(context).textTheme.bodySmall)
            else
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final e in objects)
                    _ObjectChip(
                      cls: e.key,
                      count: e.value,
                      moving: clip.movingObjects[e.key] ?? 0,
                    ),
                ],
              ),
          ],
        ),
      ),
    );
  }
}

class _ObjectChip extends StatelessWidget {
  final String cls;
  final int count;
  final int moving;
  const _ObjectChip({
    required this.cls,
    required this.count,
    required this.moving,
  });

  @override
  Widget build(BuildContext context) {
    final st = styleFor(cls);
    final label = moving > 0 ? '$count ($moving▸)' : '$count';
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: st.color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(st.icon, size: 16, color: st.color),
          const SizedBox(width: 6),
          Text(label, style: const TextStyle(fontWeight: FontWeight.w600)),
        ],
      ),
    );
  }
}
