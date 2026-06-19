import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';

import '../controller.dart';
import '../ui_helpers.dart';

class DashboardScreen extends StatelessWidget {
  final AppController controller;
  const DashboardScreen({super.key, required this.controller});

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: controller,
      builder: (context, _) {
        final c = controller;
        return RefreshIndicator(
          onRefresh: c.refresh,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
            children: [
              _ConnectionBar(controller: c),
              const SizedBox(height: 12),
              if (!c.connected && c.status == null)
                _DisconnectedHint(controller: c)
              else ...[
                _StatusCard(controller: c),
                const SizedBox(height: 16),
                _WindowSelector(controller: c),
                const SizedBox(height: 12),
                _CountsSection(controller: c),
              ],
            ],
          ),
        );
      },
    );
  }
}

class _ConnectionBar extends StatelessWidget {
  final AppController controller;
  const _ConnectionBar({required this.controller});

  @override
  Widget build(BuildContext context) {
    final c = controller;
    final color = c.connected ? Colors.green : Colors.red;
    return Row(
      children: [
        Icon(Icons.circle, size: 12, color: color),
        const SizedBox(width: 8),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                c.connected ? 'Connected' : 'Disconnected',
                style: TextStyle(fontWeight: FontWeight.bold, color: color),
              ),
              Text(
                c.backendHost,
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ],
          ),
        ),
        if (c.lastUpdated != null)
          Text(
            'updated ${timeAgo(c.lastUpdated)}',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        IconButton(
          onPressed: c.loading ? null : c.refresh,
          icon: c.loading
              ? const SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2),
                )
              : const Icon(Icons.refresh),
        ),
      ],
    );
  }
}

class _DisconnectedHint extends StatelessWidget {
  final AppController controller;
  const _DisconnectedHint({required this.controller});

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          children: [
            const Icon(Icons.wifi_off, size: 40),
            const SizedBox(height: 12),
            Text(
              'Cannot reach the AiCam backend',
              style: Theme.of(context).textTheme.titleMedium,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 8),
            Text(
              controller.error ?? 'Check that the backend is running and the '
                  'URL is correct in Settings.',
              textAlign: TextAlign.center,
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ],
        ),
      ),
    );
  }
}

class _StatusCard extends StatelessWidget {
  final AppController controller;
  const _StatusCard({required this.controller});

  @override
  Widget build(BuildContext context) {
    final s = controller.status;
    final clip = s?.lastClip;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.memory, size: 20),
                const SizedBox(width: 8),
                Text('Pipeline',
                    style: Theme.of(context).textTheme.titleMedium),
              ],
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                _Stat(
                  label: 'Worker',
                  value: (s?.workerAlive ?? false) ? 'Alive' : 'Down',
                  color: (s?.workerAlive ?? false) ? Colors.green : Colors.red,
                ),
                _Stat(label: 'Queue', value: '${s?.queueDepth ?? 0}'),
                _Stat(
                  label: 'Last clip',
                  value: clip != null ? '#${clip.id}' : '—',
                ),
                _Stat(label: 'Seen', value: timeAgo(clip?.endIso)),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _Stat extends StatelessWidget {
  final String label;
  final String value;
  final Color? color;
  const _Stat({required this.label, required this.value, this.color});

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: Column(
        children: [
          Text(
            value,
            style: Theme.of(context).textTheme.titleLarge?.copyWith(
                  color: color,
                  fontWeight: FontWeight.bold,
                ),
          ),
          const SizedBox(height: 2),
          Text(label, style: Theme.of(context).textTheme.bodySmall),
        ],
      ),
    );
  }
}

class _WindowSelector extends StatelessWidget {
  final AppController controller;
  const _WindowSelector({required this.controller});

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 8,
      children: [
        for (final w in windowOptions)
          ChoiceChip(
            label: Text(w),
            selected: controller.window == w,
            onSelected: (_) => controller.setWindow(w),
          ),
      ],
    );
  }
}

class _CountsSection extends StatelessWidget {
  final AppController controller;
  const _CountsSection({required this.controller});

  @override
  Widget build(BuildContext context) {
    final summary = controller.summary;
    // Prefer durable windowed summary; fall back to live status counts.
    final unique = summary?.unique ?? controller.status?.uniqueByClass ?? {};
    final moving = summary?.moving ?? controller.status?.movingByClass ?? {};

    if (unique.isEmpty) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 32),
        child: Center(child: Text('No objects detected in this window yet.')),
      );
    }

    final entries = unique.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));
    final headline = summary != null
        ? '${summary.clipCount} clips · ${windowLabel(controller.window)}'
        : 'Live snapshot';

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(headline, style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        _CountsChart(entries: entries),
        const SizedBox(height: 16),
        GridView.count(
          crossAxisCount: 2,
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          mainAxisSpacing: 10,
          crossAxisSpacing: 10,
          childAspectRatio: 2.2,
          children: [
            for (final e in entries)
              _CountCard(
                cls: e.key,
                unique: e.value,
                moving: moving[e.key] ?? 0,
              ),
          ],
        ),
      ],
    );
  }
}

class _CountsChart extends StatelessWidget {
  final List<MapEntry<String, int>> entries;
  const _CountsChart({required this.entries});

  @override
  Widget build(BuildContext context) {
    final shown = entries.take(6).toList();
    final maxVal =
        shown.map((e) => e.value).fold<int>(0, (a, b) => a > b ? a : b);
    return SizedBox(
      height: 180,
      child: BarChart(
        BarChartData(
          alignment: BarChartAlignment.spaceAround,
          maxY: (maxVal * 1.2).ceilToDouble().clamp(1, double.infinity),
          barTouchData: BarTouchData(
            touchTooltipData: BarTouchTooltipData(
              getTooltipItem: (group, _, rod, _) => BarTooltipItem(
                '${shown[group.x].key}\n${rod.toY.round()}',
                const TextStyle(color: Colors.white, fontSize: 12),
              ),
            ),
          ),
          titlesData: FlTitlesData(
            leftTitles:
                const AxisTitles(sideTitles: SideTitles(showTitles: false)),
            topTitles:
                const AxisTitles(sideTitles: SideTitles(showTitles: false)),
            rightTitles:
                const AxisTitles(sideTitles: SideTitles(showTitles: false)),
            bottomTitles: AxisTitles(
              sideTitles: SideTitles(
                showTitles: true,
                getTitlesWidget: (value, meta) {
                  final i = value.toInt();
                  if (i < 0 || i >= shown.length) return const SizedBox();
                  final st = styleFor(shown[i].key);
                  return Padding(
                    padding: const EdgeInsets.only(top: 6),
                    child: Icon(st.icon, size: 18, color: st.color),
                  );
                },
              ),
            ),
          ),
          gridData: const FlGridData(show: false),
          borderData: FlBorderData(show: false),
          barGroups: [
            for (var i = 0; i < shown.length; i++)
              BarChartGroupData(
                x: i,
                barRods: [
                  BarChartRodData(
                    toY: shown[i].value.toDouble(),
                    color: styleFor(shown[i].key).color,
                    width: 18,
                    borderRadius: const BorderRadius.vertical(
                      top: Radius.circular(4),
                    ),
                  ),
                ],
              ),
          ],
        ),
      ),
    );
  }
}

class _CountCard extends StatelessWidget {
  final String cls;
  final int unique;
  final int moving;
  const _CountCard({
    required this.cls,
    required this.unique,
    required this.moving,
  });

  @override
  Widget build(BuildContext context) {
    final st = styleFor(cls);
    return Card(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        child: Row(
          children: [
            CircleAvatar(
              radius: 18,
              backgroundColor: st.color.withValues(alpha: 0.18),
              child: Icon(st.icon, color: st.color, size: 20),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Text('$unique',
                      style: Theme.of(context)
                          .textTheme
                          .titleLarge
                          ?.copyWith(fontWeight: FontWeight.bold)),
                  Text(
                    '${st.label} · $moving moving',
                    style: Theme.of(context).textTheme.bodySmall,
                    overflow: TextOverflow.ellipsis,
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
