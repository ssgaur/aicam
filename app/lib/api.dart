import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

/// Live status snapshot from `GET /api/native/status`.
class LiveStatus {
  final DateTime? now;
  final bool workerAlive;
  final int queueDepth;
  final int sampledFrames;
  final int detections;
  final Map<String, int> uniqueByClass;
  final Map<String, int> movingByClass;
  final ClipInfo? lastClip;

  LiveStatus({
    required this.now,
    required this.workerAlive,
    required this.queueDepth,
    required this.sampledFrames,
    required this.detections,
    required this.uniqueByClass,
    required this.movingByClass,
    required this.lastClip,
  });

  factory LiveStatus.fromJson(Map<String, dynamic> j) {
    return LiveStatus(
      now: _parseIso(j['now']),
      workerAlive: j['worker_alive'] == true,
      queueDepth: _asInt(j['queue_depth']),
      sampledFrames: _asInt(j['sampled_frames']),
      detections: _asInt(j['detections']),
      uniqueByClass: _asCounts(j['unique_by_class']),
      movingByClass: _asCounts(j['moving_unique_by_class']),
      lastClip: j['last_clip'] is Map<String, dynamic>
          ? ClipInfo.fromStatus(j['last_clip'] as Map<String, dynamic>)
          : null,
    );
  }
}

/// Windowed summary from `GET /api/native/summary?since=...`.
class WindowSummary {
  final String since;
  final Map<String, int> unique;
  final Map<String, int> moving;
  final int clipCount;
  final List<ClipInfo> clips;

  WindowSummary({
    required this.since,
    required this.unique,
    required this.moving,
    required this.clipCount,
    required this.clips,
  });

  factory WindowSummary.fromJson(Map<String, dynamic> j) {
    final rawClips = (j['clips'] as List?) ?? const [];
    return WindowSummary(
      since: (j['since'] ?? '').toString(),
      unique: _asCounts(j['unique']),
      moving: _asCounts(j['moving']),
      clipCount: _asInt(j['clip_count']),
      clips: rawClips
          .whereType<Map<String, dynamic>>()
          .map(ClipInfo.fromSummary)
          .toList(),
    );
  }
}

/// A processed clip. Built from either the status or summary payload shapes.
class ClipInfo {
  final int id;
  final DateTime? startIso;
  final DateTime? endIso;
  final int sampledFrames;
  final Map<String, int> objects;
  final Map<String, int> movingObjects;
  final String? reportText;

  ClipInfo({
    required this.id,
    required this.startIso,
    required this.endIso,
    required this.sampledFrames,
    required this.objects,
    required this.movingObjects,
    required this.reportText,
  });

  factory ClipInfo.fromSummary(Map<String, dynamic> j) {
    return ClipInfo(
      id: _asInt(j['clip_id']),
      startIso: _parseIso(j['start_iso']),
      endIso: _parseIso(j['end_iso']),
      sampledFrames: _asInt(j['sampled_frames']),
      objects: _parseObjectString(j['objects']),
      movingObjects: _parseObjectString(j['moving_objects']),
      reportText: j['report_text']?.toString(),
    );
  }

  factory ClipInfo.fromStatus(Map<String, dynamic> j) {
    return ClipInfo(
      id: _asInt(j['id']),
      startIso: _parseIso(j['start_iso']),
      endIso: _parseIso(j['end_iso']),
      sampledFrames: _asInt(j['sampled_frames']),
      objects: const {},
      movingObjects: const {},
      reportText: null,
    );
  }
}

class ApiException implements Exception {
  final String message;
  ApiException(this.message);
  @override
  String toString() => message;
}

/// Talks to the AiCam backend over LAN.
class ApiClient {
  String baseUrl;
  final Duration timeout;

  ApiClient(this.baseUrl, {this.timeout = const Duration(seconds: 6)});

  String get _root => baseUrl.trim().replaceAll(RegExp(r'/+$'), '');

  Future<bool> ping() async {
    try {
      await _getJson('/api/native/status');
      return true;
    } catch (_) {
      return false;
    }
  }

  Future<LiveStatus> fetchStatus() async {
    final j = await _getJson('/api/native/status');
    return LiveStatus.fromJson(j);
  }

  Future<WindowSummary> fetchSummary(String since) async {
    final j = await _getJson('/api/native/summary?since=$since');
    return WindowSummary.fromJson(j);
  }

  Future<Map<String, dynamic>> _getJson(String path) async {
    final uri = Uri.parse('$_root$path');
    final http.Response res;
    try {
      res = await http.get(uri).timeout(timeout);
    } on TimeoutException {
      throw ApiException('Timed out reaching $_root');
    } catch (e) {
      throw ApiException('Cannot reach $_root');
    }
    if (res.statusCode != 200) {
      throw ApiException('HTTP ${res.statusCode} from $path');
    }
    final decoded = jsonDecode(res.body);
    if (decoded is! Map<String, dynamic>) {
      throw ApiException('Unexpected response from $path');
    }
    return decoded;
  }
}

// ---- helpers ----

int _asInt(dynamic v) {
  if (v is int) return v;
  if (v is double) return v.round();
  if (v is String) return int.tryParse(v) ?? 0;
  return 0;
}

Map<String, int> _asCounts(dynamic v) {
  if (v is Map) {
    final out = <String, int>{};
    v.forEach((k, val) => out[k.toString()] = _asInt(val));
    return out;
  }
  return {};
}

DateTime? _parseIso(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString());
}

/// Parses "person:6, car:2, motorcycle:1" → {person:6, car:2, motorcycle:1}.
Map<String, int> _parseObjectString(dynamic v) {
  if (v is Map) return _asCounts(v);
  if (v is! String || v.trim().isEmpty) return {};
  final out = <String, int>{};
  for (final part in v.split(',')) {
    final kv = part.split(':');
    if (kv.length == 2) {
      final key = kv[0].trim();
      final n = int.tryParse(kv[1].trim());
      if (key.isNotEmpty && n != null) out[key] = n;
    } else {
      final key = part.trim();
      if (key.isNotEmpty) out[key] = 1;
    }
  }
  return out;
}
