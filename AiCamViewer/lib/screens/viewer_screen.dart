import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../models/clip.dart';
import '../services/api.dart';
import 'clip_player_screen.dart';

class ViewerScreen extends StatefulWidget {
  final AicamApi api;
  const ViewerScreen({super.key, required this.api});

  @override
  State<ViewerScreen> createState() => _ViewerScreenState();
}

class _ViewerScreenState extends State<ViewerScreen> {
  List<Clip> _clips = [];
  bool _loading = true;
  String? _error;
  int _windowMinutes = 5;
  int _totalClips = 0;

  final List<int> _windowOptions = [5, 15, 30, 60, 240, 720, 1440];

  @override
  void initState() {
    super.initState();
    _loadClips();
  }

  Future<void> _loadClips() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final clips = await widget.api.getClips(minutes: _windowMinutes);
      final range = await widget.api.getRange();
      setState(() {
        _clips = clips;
        _totalClips = range.totalClips;
        _loading = false;
      });
    } catch (e) {
      setState(() {
        _error = e.toString();
        _loading = false;
      });
    }
  }

  String _windowLabel(int min) {
    if (min < 60) return '${min}m';
    if (min < 1440) return '${min ~/ 60}h';
    return '24h';
  }

  String _formatTime(double ts) {
    final dt = DateTime.fromMillisecondsSinceEpoch((ts * 1000).toInt());
    return DateFormat('hh:mm:ss a').format(dt);
  }

  Future<void> _deleteClip(Clip clip) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete Clip'),
        content: Text('Delete clip #${clip.id}?'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: const Text('Cancel')),
          TextButton(
              onPressed: () => Navigator.pop(ctx, true),
              style: TextButton.styleFrom(foregroundColor: Colors.red),
              child: const Text('Delete')),
        ],
      ),
    );
    if (confirmed == true) {
      final ok = await widget.api.deleteClip(clip.id);
      if (ok) {
        setState(() => _clips.removeWhere((c) => c.id == clip.id));
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
              SnackBar(content: Text('Clip #${clip.id} deleted')));
        }
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF0A0C12),
      appBar: AppBar(
        backgroundColor: const Color(0xFF141820),
        title: Row(
          children: [
            Container(
              width: 8,
              height: 8,
              decoration: const BoxDecoration(
                  color: Color(0xFF4ADE80), shape: BoxShape.circle),
            ),
            const SizedBox(width: 8),
            const Text('AiCam Viewer',
                style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
            const Spacer(),
            Text('$_totalClips clips',
                style:
                    const TextStyle(fontSize: 12, color: Color(0xFF6E7490))),
          ],
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh, size: 20),
            onPressed: _loadClips,
          ),
        ],
      ),
      body: Column(
        children: [
          // Time window selector
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
            color: const Color(0xFF141820),
            child: SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: Row(
                children: _windowOptions.map((min) {
                  final active = min == _windowMinutes;
                  return Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ChoiceChip(
                      label: Text(_windowLabel(min)),
                      selected: active,
                      selectedColor: const Color(0xFF4F8CFF),
                      backgroundColor: const Color(0xFF1A1F2A),
                      labelStyle: TextStyle(
                        color: active ? Colors.black : Colors.white,
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                      ),
                      side: BorderSide.none,
                      onSelected: (_) {
                        setState(() => _windowMinutes = min);
                        _loadClips();
                      },
                    ),
                  );
                }).toList(),
              ),
            ),
          ),
          // Coverage banner
          if (!_loading && _clips.isNotEmpty)
            Container(
              margin: const EdgeInsets.fromLTRB(12, 8, 12, 0),
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
              decoration: BoxDecoration(
                color: const Color(0xFF1A1F2A),
                borderRadius: BorderRadius.circular(10),
              ),
              child: Row(
                children: [
                  _StatChip(
                      label: 'Total',
                      value: '${_clips.length}',
                      color: const Color(0xFF4F8CFF)),
                  const SizedBox(width: 12),
                  _StatChip(
                      label: 'Active',
                      value:
                          '${_clips.where((c) => c.hasActivity).length}',
                      color: const Color(0xFF4ADE80)),
                  const SizedBox(width: 12),
                  _StatChip(
                      label: 'Empty',
                      value:
                          '${_clips.where((c) => !c.hasActivity).length}',
                      color: const Color(0xFFFB923C)),
                ],
              ),
            ),
          // Clip grid
          Expanded(
            child: _loading
                ? const Center(
                    child: CircularProgressIndicator(
                        color: Color(0xFF4F8CFF)))
                : _error != null
                    ? Center(
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            const Icon(Icons.error_outline,
                                color: Color(0xFFF87171), size: 48),
                            const SizedBox(height: 12),
                            Text(_error!,
                                style: const TextStyle(
                                    color: Color(0xFF6E7490)),
                                textAlign: TextAlign.center),
                            const SizedBox(height: 12),
                            ElevatedButton(
                                onPressed: _loadClips,
                                child: const Text('Retry')),
                          ],
                        ),
                      )
                    : _clips.isEmpty
                        ? const Center(
                            child: Text('No clips in this window',
                                style:
                                    TextStyle(color: Color(0xFF6E7490))))
                        : RefreshIndicator(
                            onRefresh: _loadClips,
                            child: GridView.builder(
                              padding: const EdgeInsets.all(12),
                              gridDelegate:
                                  const SliverGridDelegateWithFixedCrossAxisCount(
                                crossAxisCount: 2,
                                childAspectRatio: 0.72,
                                crossAxisSpacing: 10,
                                mainAxisSpacing: 10,
                              ),
                              itemCount: _clips.length,
                              itemBuilder: (ctx, i) =>
                                  _ClipCard(
                                    clip: _clips[i],
                                    api: widget.api,
                                    onTap: () => _openClip(i),
                                    onDelete: () => _deleteClip(_clips[i]),
                                    formatTime: _formatTime,
                                  ),
                            ),
                          ),
          ),
        ],
      ),
    );
  }

  void _openClip(int index) {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) => ClipPlayerScreen(
          clips: _clips,
          initialIndex: index,
          api: widget.api,
        ),
      ),
    );
  }
}

class _StatChip extends StatelessWidget {
  final String label;
  final String value;
  final Color color;

  const _StatChip(
      {required this.label, required this.value, required this.color});

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(value,
            style: TextStyle(
                color: color, fontSize: 18, fontWeight: FontWeight.w700)),
        Text(label,
            style:
                const TextStyle(color: Color(0xFF6E7490), fontSize: 10)),
      ],
    );
  }
}

class _ClipCard extends StatelessWidget {
  final Clip clip;
  final AicamApi api;
  final VoidCallback onTap;
  final VoidCallback onDelete;
  final String Function(double) formatTime;

  const _ClipCard({
    required this.clip,
    required this.api,
    required this.onTap,
    required this.onDelete,
    required this.formatTime,
  });

  @override
  Widget build(BuildContext context) {
    final empty = !clip.hasActivity && clip.objects.isEmpty;
    return GestureDetector(
      onTap: onTap,
      child: Container(
        decoration: BoxDecoration(
          color: const Color(0xFF141820),
          borderRadius: BorderRadius.circular(10),
          border: empty
              ? Border.all(color: const Color(0xFFF87171), width: 2)
              : null,
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Thumbnail + badge
            Expanded(
              child: Stack(
                fit: StackFit.expand,
                children: [
                  ClipRRect(
                    borderRadius:
                        const BorderRadius.vertical(top: Radius.circular(10)),
                    child: Image.network(
                      api.thumbUrl(clip.id),
                      fit: BoxFit.cover,
                      errorBuilder: (_, e, s) => Container(
                        color: const Color(0xFF1A1F2A),
                        child: const Center(
                            child: Icon(Icons.videocam_off,
                                color: Color(0xFF6E7490), size: 32)),
                      ),
                    ),
                  ),
                  // Badge
                  Positioned(
                    top: 6,
                    right: 6,
                    child: Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 7, vertical: 2),
                      decoration: BoxDecoration(
                        color: clip.hasActivity
                            ? const Color(0xFF4ADE80).withValues(alpha: 0.85)
                            : const Color(0xFFFB923C).withValues(alpha: 0.85),
                        borderRadius: BorderRadius.circular(10),
                      ),
                      child: Text(
                        clip.hasActivity ? 'ACTIVE' : 'NO ACTIVITY',
                        style: const TextStyle(
                            fontSize: 8,
                            fontWeight: FontWeight.w700,
                            color: Colors.black),
                      ),
                    ),
                  ),
                  // Delete button
                  Positioned(
                    top: 4,
                    left: 4,
                    child: GestureDetector(
                      onTap: onDelete,
                      child: Container(
                        width: 22,
                        height: 22,
                        decoration: BoxDecoration(
                          color: const Color(0xFFF87171).withValues(alpha: 0.85),
                          shape: BoxShape.circle,
                        ),
                        child: const Icon(Icons.close,
                            size: 14, color: Colors.white),
                      ),
                    ),
                  ),
                ],
              ),
            ),
            // Info
            Padding(
              padding: const EdgeInsets.all(8),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Container(
                        padding: const EdgeInsets.symmetric(
                            horizontal: 6, vertical: 2),
                        decoration: BoxDecoration(
                          color: const Color(0xFF7C5CFF),
                          borderRadius: BorderRadius.circular(4),
                        ),
                        child: Text('#${clip.id}',
                            style: const TextStyle(
                                fontSize: 10,
                                fontWeight: FontWeight.w700,
                                color: Colors.white)),
                      ),
                      const Spacer(),
                      Text(
                          '${clip.durationSec?.toStringAsFixed(1) ?? '~10'}s',
                          style: const TextStyle(
                              fontSize: 9, color: Color(0xFF6E7490))),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(formatTime(clip.startTs),
                      style: const TextStyle(
                          fontSize: 12, color: Colors.white)),
                  if (clip.objects.isNotEmpty) ...[
                    const SizedBox(height: 4),
                    Wrap(
                      spacing: 4,
                      runSpacing: 2,
                      children: clip.objects.entries.map((e) {
                        return Container(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 5, vertical: 1),
                          decoration: BoxDecoration(
                            border: Border.all(
                                color: const Color(0xFF4F8CFF), width: 1),
                            borderRadius: BorderRadius.circular(8),
                          ),
                          child: Text('${e.key}×${e.value}',
                              style: const TextStyle(
                                  fontSize: 8,
                                  color: Color(0xFF4F8CFF))),
                        );
                      }).toList(),
                    ),
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
