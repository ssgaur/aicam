class Clip {
  final int id;
  final double startTs;
  final double endTs;
  final String startIso;
  final String endIso;
  final double? durationSec;
  final int? sampledFrames;
  final String status;
  final bool hasActivity;
  final Map<String, int> objects;

  Clip({
    required this.id,
    required this.startTs,
    required this.endTs,
    required this.startIso,
    required this.endIso,
    this.durationSec,
    this.sampledFrames,
    required this.status,
    required this.hasActivity,
    required this.objects,
  });

  factory Clip.fromJson(Map<String, dynamic> json) {
    final objs = <String, int>{};
    if (json['objects'] is Map) {
      (json['objects'] as Map).forEach((k, v) {
        objs[k.toString()] = (v is int) ? v : (v as num).toInt();
      });
    }
    return Clip(
      id: json['id'] as int,
      startTs: (json['start_ts'] as num).toDouble(),
      endTs: (json['end_ts'] as num).toDouble(),
      startIso: json['start_iso'] as String? ?? '',
      endIso: json['end_iso'] as String? ?? '',
      durationSec: (json['duration_sec'] as num?)?.toDouble(),
      sampledFrames: json['sampled_frames'] as int?,
      status: json['status'] as String? ?? '',
      hasActivity: json['has_activity'] as bool? ?? false,
      objects: objs,
    );
  }
}

class ClipRange {
  final double? minTs;
  final double? maxTs;
  final int totalClips;

  ClipRange({this.minTs, this.maxTs, required this.totalClips});

  factory ClipRange.fromJson(Map<String, dynamic> json) {
    return ClipRange(
      minTs: (json['min_ts'] as num?)?.toDouble(),
      maxTs: (json['max_ts'] as num?)?.toDouble(),
      totalClips: json['total_clips'] as int? ?? 0,
    );
  }
}
