import 'dart:io';
import 'package:flutter/material.dart';
import 'package:video_player/video_player.dart';
import 'package:share_plus/share_plus.dart';
import 'package:path_provider/path_provider.dart';
import 'package:dio/dio.dart';
import 'package:dio/io.dart';
import '../models/clip.dart';
import '../services/api.dart';

class ClipPlayerScreen extends StatefulWidget {
  final List<Clip> clips;
  final int initialIndex;
  final AicamApi api;

  const ClipPlayerScreen({
    super.key,
    required this.clips,
    required this.initialIndex,
    required this.api,
  });

  @override
  State<ClipPlayerScreen> createState() => _ClipPlayerScreenState();
}

class _ClipPlayerScreenState extends State<ClipPlayerScreen> {
  VideoPlayerController? _controller;
  bool _loading = true;
  bool _downloading = false;
  double _downloadProgress = 0;
  String? _error;
  late int _currentIndex;

  Clip get _clip => widget.clips[_currentIndex];
  bool get _hasPrev => _currentIndex > 0;
  bool get _hasNext => _currentIndex < widget.clips.length - 1;

  @override
  void initState() {
    super.initState();
    _currentIndex = widget.initialIndex;
    _initPlayer();
  }

  Dio get _dio {
    final dio = Dio();
    (dio.httpClientAdapter as IOHttpClientAdapter).createHttpClient = () {
      final client = HttpClient();
      client.badCertificateCallback = (cert, host, port) => true;
      return client;
    };
    dio.options.followRedirects = true;
    dio.options.maxRedirects = 5;
    return dio;
  }

  Future<void> _initPlayer() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    _controller?.dispose();
    _controller = null;

    try {
      final dir = await getTemporaryDirectory();
      final path = '${dir.path}/clip_${_clip.id}.mp4';
      final file = File(path);

      if (!file.existsSync()) {
        await _dio.download(widget.api.videoUrl(_clip.id), path);
      }

      _controller = VideoPlayerController.file(file);
      await _controller!.initialize();
      _controller!.setLooping(true);
      _controller!.play();
      setState(() => _loading = false);
    } catch (e) {
      setState(() {
        _error = 'Could not load video: $e';
        _loading = false;
      });
    }
  }

  void _goNext() {
    if (_hasNext) {
      _currentIndex++;
      _initPlayer();
    }
  }

  void _goPrev() {
    if (_hasPrev) {
      _currentIndex--;
      _initPlayer();
    }
  }

  Future<void> _downloadClip() async {
    setState(() {
      _downloading = true;
      _downloadProgress = 0;
    });
    try {
      final dir = await getApplicationDocumentsDirectory();
      final path = '${dir.path}/aicam_clip_${_clip.id}.mp4';

      await _dio.download(
        widget.api.videoUrl(_clip.id),
        path,
        onReceiveProgress: (received, total) {
          if (total > 0) {
            setState(() => _downloadProgress = received / total);
          }
        },
      );

      setState(() => _downloading = false);

      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text('Downloaded clip #${_clip.id}'),
            action: SnackBarAction(
              label: 'Share',
              onPressed: () => _shareFile(path),
            ),
          ),
        );
      }
    } catch (e) {
      setState(() => _downloading = false);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Download failed: $e')),
        );
      }
    }
  }

  Future<void> _shareFile(String path) async {
    await SharePlus.instance.share(ShareParams(files: [XFile(path)],
        text: 'AiCam clip #${_clip.id}'));
  }

  @override
  void dispose() {
    _controller?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        backgroundColor: const Color(0xFF141820),
        title: Text('Clip #${_clip.id}',
            style: const TextStyle(fontSize: 16)),
        actions: [
          if (_downloading)
            Padding(
              padding: const EdgeInsets.all(12),
              child: SizedBox(
                width: 24,
                height: 24,
                child: CircularProgressIndicator(
                  value: _downloadProgress > 0 ? _downloadProgress : null,
                  strokeWidth: 2,
                  color: const Color(0xFF4ADE80),
                ),
              ),
            )
          else
            IconButton(
              icon: const Icon(Icons.download, color: Color(0xFF4ADE80)),
              onPressed: _downloadClip,
              tooltip: 'Download clip',
            ),
        ],
      ),
      body: Column(
        children: [
          // Video player with prev/next overlay
          Expanded(
            flex: 3,
            child: Stack(
              children: [
                // Video
                Center(
                  child: _loading
                      ? const CircularProgressIndicator(
                          color: Color(0xFF4F8CFF))
                      : _error != null
                          ? Column(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                const Icon(Icons.error_outline,
                                    color: Color(0xFFF87171), size: 48),
                                const SizedBox(height: 12),
                                Text(_error!,
                                    style: const TextStyle(
                                        color: Color(0xFF6E7490)),
                                    textAlign: TextAlign.center),
                              ],
                            )
                          : GestureDetector(
                              onTap: () {
                                if (_controller!.value.isPlaying) {
                                  _controller!.pause();
                                } else {
                                  _controller!.play();
                                }
                                setState(() {});
                              },
                              child: InteractiveViewer(
                                minScale: 1.0,
                                maxScale: 5.0,
                                child: Stack(
                                  alignment: Alignment.center,
                                  children: [
                                    AspectRatio(
                                      aspectRatio:
                                          _controller!.value.aspectRatio,
                                      child: VideoPlayer(_controller!),
                                    ),
                                    if (!_controller!.value.isPlaying)
                                      Container(
                                        width: 64,
                                        height: 64,
                                        decoration: BoxDecoration(
                                          color: Colors.black
                                              .withValues(alpha: 0.5),
                                          shape: BoxShape.circle,
                                        ),
                                        child: const Icon(Icons.play_arrow,
                                            color: Colors.white, size: 40),
                                      ),
                                  ],
                                ),
                              ),
                            ),
                ),
                // Left arrow (prev)
                if (_hasPrev)
                  Positioned(
                    left: 8,
                    top: 0,
                    bottom: 0,
                    child: Center(
                      child: IconButton(
                        onPressed: _goPrev,
                        icon: const Icon(Icons.chevron_left,
                            color: Colors.white, size: 40),
                        style: IconButton.styleFrom(
                          backgroundColor: Colors.black54,
                        ),
                      ),
                    ),
                  ),
                // Right arrow (next)
                if (_hasNext)
                  Positioned(
                    right: 8,
                    top: 0,
                    bottom: 0,
                    child: Center(
                      child: IconButton(
                        onPressed: _goNext,
                        icon: const Icon(Icons.chevron_right,
                            color: Colors.white, size: 40),
                        style: IconButton.styleFrom(
                          backgroundColor: Colors.black54,
                        ),
                      ),
                    ),
                  ),
                // Position indicator
                Positioned(
                  bottom: 8,
                  left: 0,
                  right: 0,
                  child: Center(
                    child: Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 10, vertical: 4),
                      decoration: BoxDecoration(
                        color: Colors.black54,
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: Text(
                        '${_currentIndex + 1} / ${widget.clips.length}',
                        style: const TextStyle(
                            color: Colors.white, fontSize: 12),
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
          // Progress bar
          if (_controller != null && _controller!.value.isInitialized)
            VideoProgressIndicator(
              _controller!,
              allowScrubbing: true,
              colors: const VideoProgressColors(
                playedColor: Color(0xFF4F8CFF),
                bufferedColor: Color(0xFF1A1F2A),
                backgroundColor: Color(0xFF252A38),
              ),
            ),
          // Clip details
          Expanded(
            flex: 2,
            child: Container(
              width: double.infinity,
              padding: const EdgeInsets.all(16),
              color: const Color(0xFF141820),
              child: SingleChildScrollView(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    // Activity badge
                    Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 10, vertical: 4),
                      decoration: BoxDecoration(
                        color: _clip.hasActivity
                            ? const Color(0xFF4ADE80).withValues(alpha: 0.85)
                            : const Color(0xFFFB923C).withValues(alpha: 0.85),
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: Text(
                        _clip.hasActivity ? 'ACTIVE' : 'NO ACTIVITY',
                        style: const TextStyle(
                            fontSize: 11,
                            fontWeight: FontWeight.w700,
                            color: Colors.black),
                      ),
                    ),
                    const SizedBox(height: 12),
                    _InfoRow(label: 'Time', value: _clip.startIso),
                    _InfoRow(
                        label: 'Duration',
                        value:
                            '${_clip.durationSec?.toStringAsFixed(1) ?? "~10"}s'),
                    _InfoRow(
                        label: 'Frames',
                        value: '${_clip.sampledFrames ?? 0}'),
                    const SizedBox(height: 12),
                    if (_clip.objects.isNotEmpty) ...[
                      const Text('Detected Objects',
                          style: TextStyle(
                              color: Color(0xFF6E7490),
                              fontSize: 11,
                              fontWeight: FontWeight.w600)),
                      const SizedBox(height: 6),
                      Wrap(
                        spacing: 8,
                        runSpacing: 6,
                        children: _clip.objects.entries.map((e) {
                          return Container(
                            padding: const EdgeInsets.symmetric(
                                horizontal: 10, vertical: 4),
                            decoration: BoxDecoration(
                              border: Border.all(
                                  color: const Color(0xFF4ADE80), width: 1),
                              borderRadius: BorderRadius.circular(12),
                            ),
                            child: Text('${e.key} × ${e.value}',
                                style: const TextStyle(
                                    fontSize: 12,
                                    color: Color(0xFF4ADE80))),
                          );
                        }).toList(),
                      ),
                    ],
                  ],
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _InfoRow extends StatelessWidget {
  final String label;
  final String value;

  const _InfoRow({required this.label, required this.value});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        children: [
          SizedBox(
            width: 70,
            child: Text(label,
                style:
                    const TextStyle(color: Color(0xFF6E7490), fontSize: 12)),
          ),
          Expanded(
            child: Text(value,
                style: const TextStyle(color: Colors.white, fontSize: 12)),
          ),
        ],
      ),
    );
  }
}
