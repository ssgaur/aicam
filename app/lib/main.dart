import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'package:camera/camera.dart';
import 'package:flutter/material.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'package:http/http.dart' as http;
import 'package:network_info_plus/network_info_plus.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

const kDefaultBackend = 'http://192.168.1.72:8100';

late List<CameraDescription> _cameras;

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    _cameras = await availableCameras();
  } catch (_) {
    _cameras = [];
  }
  runApp(const AiCamApp());
}

class AiCamApp extends StatelessWidget {
  const AiCamApp({super.key});
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'AiCam',
      theme: ThemeData(colorSchemeSeed: Colors.deepPurple, useMaterial3: true, brightness: Brightness.dark),
      home: const HomeScreen(),
    );
  }
}

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});
  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> with WidgetsBindingObserver {
  final TextEditingController _backendCtl = TextEditingController(text: kDefaultBackend);
  CameraController? _cam;
  bool _camReady = false;
  bool? _pingOk;
  bool _streaming = false;
  bool _chunking = false;
  bool _chunkRecording = false;
  WebSocketChannel? _ws;
  Uint8List? _overlay;
  int _fps = 0;
  int _framesSent = 0;
  int _framesRecv = 0;
  DateTime _lastSec = DateTime.now();
  int _recvInLastSec = 0;
  bool _busy = false;
  bool _scanning = false;
  String _status = '';
  String _chunkStatus = '';
  int _chunkIndex = 0;
  int _chunksUploaded = 0;
  int _chunkErrors = 0;
  final TextEditingController _chunkSecondsCtl = TextEditingController(text: '10');
  final TextEditingController _sampleFpsCtl = TextEditingController(text: '2');
  final FlutterTts _tts = FlutterTts();
  int _lastSayId = 0;
  Timer? _sayTimer;
  String _lastSpoken = '';

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _initCamera();
    _initTts();
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      await _autoDiscover();
      _startSayPoll();
    });
  }

  Future<void> _initTts() async {
    await _tts.setLanguage('en-US');
    await _tts.setSpeechRate(0.5);
    await _tts.setPitch(1.0);
    await _tts.setVolume(1.0);
  }

  void _startSayPoll() {
    _sayTimer?.cancel();
    _sayTimer = Timer.periodic(const Duration(seconds: 2), (_) => _pollSay());
  }

  Future<void> _pollSay() async {
    if (_pingOk != true) return;
    try {
      final r = await http
          .get(Uri.parse('${_backendCtl.text}/api/say/pending?since=$_lastSayId'))
          .timeout(const Duration(seconds: 3));
      if (r.statusCode != 200) return;
      final list = jsonDecode(r.body) as List<dynamic>;
      for (final m in list) {
        final id = m['id'] as int;
        final text = (m['text'] as String?) ?? '';
        if (id > _lastSayId) _lastSayId = id;
        if (text.trim().isEmpty) continue;
        await _tts.speak(text);
        if (mounted) setState(() => _lastSpoken = text);
        // wait approximately for speech to finish before next
        await Future.delayed(Duration(milliseconds: 300 + text.length * 60));
      }
    } catch (_) {}
  }

  Future<void> _autoDiscover() async {
    setState(() {
      _scanning = true;
      _status = 'Scanning LAN…';
    });
    try {
      // First try the saved/default URL
      if (await _ping(_backendCtl.text)) {
        await _setBackendAndPing(_backendCtl.text, 'Backend connected');
        return;
      }
      // Get phone's own IP, scan its /24
      final info = NetworkInfo();
      final ip = await info.getWifiIP();
      if (ip == null || !ip.contains('.')) {
        setState(() {
          _scanning = false;
          _status = 'No Wi-Fi IP — set backend manually';
        });
        return;
      }
      final parts = ip.split('.');
      final prefix = '${parts[0]}.${parts[1]}.${parts[2]}';
      // Ping all 254 hosts in parallel
      final futures = <Future<String?>>[];
      for (var i = 1; i < 255; i++) {
        final candidate = 'http://$prefix.$i:8100';
        futures.add(_pingReturn(candidate));
      }
      final results = await Future.wait(futures);
      String? found;
      for (final r in results) {
        if (r != null) {
          found = r;
          break;
        }
      }
      if (found != null && mounted) {
        await _setBackendAndPing(found, 'Backend auto-discovered');
      } else if (mounted) {
        setState(() {
          _pingOk = false;
          _scanning = false;
          _status = 'Backend not found on LAN — set manually';
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _scanning = false;
          _status = 'Scan error: $e';
        });
      }
    }
  }

  Future<bool> _ping(String url) async {
    try {
      final r = await http.get(Uri.parse('$url/healthz')).timeout(const Duration(milliseconds: 800));
      return r.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  Future<void> _setBackendAndPing(String url, String successStatus) async {
    _backendCtl.text = url;
    final ok = await _ping(url);
    if (!mounted) return;
    setState(() {
      _pingOk = ok;
      _scanning = false;
      _status = ok ? successStatus : 'Backend not reachable — set manually';
    });
  }

  Future<String?> _pingReturn(String url) async {
    try {
      final r = await http.get(Uri.parse('$url/healthz')).timeout(const Duration(milliseconds: 600));
      if (r.statusCode == 200) return url;
    } catch (_) {}
    return null;
  }

  void _onBackendChanged(String value) {
    setState(() {
      _pingOk = null;
      _status = 'Backend URL changed — tap Test';
    });
  }

  Future<void> _initCamera() async {
    if (_cameras.isEmpty) {
      setState(() => _status = 'No camera found');
      return;
    }
    final back = _cameras.firstWhere(
      (c) => c.lensDirection == CameraLensDirection.back,
      orElse: () => _cameras.first,
    );
    final c = CameraController(back, ResolutionPreset.high, enableAudio: false, imageFormatGroup: ImageFormatGroup.jpeg);
    await c.initialize();
    if (!mounted) return;
    setState(() {
      _cam = c;
      _camReady = true;
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _sayTimer?.cancel();
    _tts.stop();
    _stopStream();
    _stopChunking();
    _cam?.dispose();
    _backendCtl.dispose();
    _chunkSecondsCtl.dispose();
    _sampleFpsCtl.dispose();
    super.dispose();
  }

  Future<void> _testBackend() async {
    setState(() {
      _busy = true;
      _status = 'Testing backend…';
    });
    try {
      final r = await http.get(Uri.parse('${_backendCtl.text}/healthz')).timeout(const Duration(seconds: 4));
      setState(() {
        _pingOk = r.statusCode == 200;
        _status = r.statusCode == 200 ? 'Backend connected' : 'Backend test failed (${r.statusCode})';
      });
    } catch (_) {
      setState(() {
        _pingOk = false;
        _status = 'Backend not reachable — set manually';
      });
    } finally {
      setState(() => _busy = false);
    }
  }

  Future<void> _toggleStream() async {
    if (_streaming) {
      await _stopStream();
    } else {
      await _startStream();
    }
  }

  Future<void> _toggleChunking() async {
    if (_chunking) {
      await _stopChunking();
    } else {
      await _startChunking();
    }
  }

  Future<void> _startChunking() async {
    if (!_camReady || _cam == null || _chunking) return;
    if (_streaming) await _stopStream();
    if (_cam!.value.isTakingPicture || _cam!.value.isRecordingVideo) {
      setState(() => _chunkStatus = 'Camera busy');
      return;
    }
    setState(() {
      _chunking = true;
      _chunkStatus = 'Starting chunks…';
      _chunkIndex = 0;
      _chunksUploaded = 0;
      _chunkErrors = 0;
    });
    unawaited(_chunkLoop());
  }

  Future<void> _stopChunking() async {
    if (!_chunking && !_chunkRecording) return;
    setState(() {
      _chunking = false;
      _chunkStatus = _chunkRecording ? 'Stopping after current chunk…' : 'Stopped';
    });
  }

  int _chunkSeconds() {
    final parsed = int.tryParse(_chunkSecondsCtl.text.trim()) ?? 10;
    return parsed.clamp(3, 300).toInt();
  }

  double _sampleFps() {
    final parsed = double.tryParse(_sampleFpsCtl.text.trim()) ?? 2.0;
    return parsed.clamp(0.2, 10.0).toDouble();
  }

  Future<void> _chunkLoop() async {
    while (_chunking && mounted && _cam != null) {
      final chunkNo = _chunkIndex + 1;
      final seconds = _chunkSeconds();
      XFile? recorded;
      late DateTime started;
      late DateTime stopped;
      try {
        try {
          await _cam!.prepareForVideoRecording();
        } catch (_) {}
        started = DateTime.now();
        await _cam!.startVideoRecording();
        _chunkRecording = true;
        if (mounted) {
          setState(() {
            _chunkIndex = chunkNo;
            _chunkStatus = 'Recording chunk #$chunkNo (${seconds}s)…';
          });
        }

        final deadline = started.add(Duration(seconds: seconds));
        while (_chunking && DateTime.now().isBefore(deadline)) {
          await Future.delayed(const Duration(milliseconds: 250));
        }

        recorded = await _cam!.stopVideoRecording();
        stopped = DateTime.now();
        _chunkRecording = false;
        if (mounted) setState(() => _chunkStatus = 'Uploading chunk #$chunkNo…');
      } catch (e) {
        _chunkRecording = false;
        _chunkErrors++;
        if (mounted) setState(() => _chunkStatus = 'Chunk #$chunkNo error: $e');
        try {
          if (_cam?.value.isRecordingVideo == true) {
            await _cam!.stopVideoRecording();
          }
        } catch (_) {}
        await Future.delayed(const Duration(seconds: 2));
        continue;
      }

      unawaited(_uploadNativeClip(recorded, started, stopped, chunkNo));
      await Future.delayed(const Duration(milliseconds: 300));
    }
    if (mounted) {
      setState(() {
        _chunking = false;
        _chunkRecording = false;
        if (!_chunkStatus.startsWith('Chunk')) _chunkStatus = 'Stopped';
      });
    }
  }

  Future<void> _uploadNativeClip(XFile clip, DateTime started, DateTime stopped, int chunkNo) async {
    try {
      final uri = Uri.parse('${_backendCtl.text}/api/native/upload');
      final req = http.MultipartRequest('POST', uri);
      req.fields['start_ts'] = (started.millisecondsSinceEpoch / 1000.0).toStringAsFixed(3);
      req.fields['end_ts'] = (stopped.millisecondsSinceEpoch / 1000.0).toStringAsFixed(3);
      req.fields['sample_fps'] = _sampleFps().toString();
      req.fields['conf'] = '0.35';
      req.fields['chunk_index'] = chunkNo.toString();
      req.fields['device_id'] = 'flutter-camerax';
      req.files.add(await http.MultipartFile.fromPath('file', clip.path, filename: 'aicam_chunk_$chunkNo.mp4'));
      final streamed = await req.send().timeout(const Duration(minutes: 3));
      final body = await streamed.stream.bytesToString();
      if (streamed.statusCode < 200 || streamed.statusCode >= 300) {
        throw Exception('HTTP ${streamed.statusCode}: $body');
      }
      _chunksUploaded++;
      try {
        await File(clip.path).delete();
      } catch (_) {}
      if (mounted) setState(() => _chunkStatus = 'Uploaded chunk #$chunkNo ($_chunksUploaded ok, $_chunkErrors errors)');
    } catch (e) {
      _chunkErrors++;
      if (mounted) setState(() => _chunkStatus = 'Upload #$chunkNo failed: $e');
    }
  }

  Future<void> _startStream() async {
    if (!_camReady) return;
    final url = '${_backendCtl.text.replaceFirst('http', 'ws')}/ws/segment';
    try {
      _ws = WebSocketChannel.connect(Uri.parse(url));
      _ws!.stream.listen(_onMessage, onError: (_) => _stopStream(), onDone: _stopStream);
    } catch (e) {
      setState(() => _status = 'WS error: $e');
      return;
    }
    setState(() {
      _streaming = true;
      _status = 'streaming';
      _framesSent = 0;
      _framesRecv = 0;
    });
    _loopSendFrames();
  }

  Future<void> _stopStream() async {
    final ws = _ws;
    _ws = null;
    try {
      await ws?.sink.close();
    } catch (_) {}
    if (mounted) {
      setState(() {
        _streaming = false;
        _status = 'stopped';
      });
    }
  }

  Future<void> _loopSendFrames() async {
    while (_streaming && _ws != null && _cam != null) {
      try {
        final pic = await _cam!.takePicture();
        final bytes = await pic.readAsBytes();
        if (!_streaming) break;
        _ws!.sink.add(bytes);
        _framesSent++;
      } catch (e) {
        await Future.delayed(const Duration(milliseconds: 200));
      }
      // Throttle to ~5 FPS max
      await Future.delayed(const Duration(milliseconds: 200));
    }
  }

  void _onMessage(dynamic msg) {
    Uint8List? bytes;
    if (msg is List<int>) {
      bytes = Uint8List.fromList(msg);
    } else if (msg is String) {
      try {
        final m = jsonDecode(msg) as Map<String, dynamic>;
        if (m['png_b64'] is String) {
          bytes = base64Decode(m['png_b64']);
        }
      } catch (_) {}
    }
    if (bytes == null) return;
    _framesRecv++;
    _recvInLastSec++;
    final now = DateTime.now();
    if (now.difference(_lastSec).inMilliseconds >= 1000) {
      _fps = _recvInLastSec;
      _recvInLastSec = 0;
      _lastSec = now;
    }
    // ignore tiny ack PNGs (< 500 bytes = transparent acknowledgement, no overlay to draw)
    if (bytes.length < 500) {
      if (_overlay != null && mounted) setState(() => _overlay = null);
      return;
    }
    if (mounted) setState(() => _overlay = bytes);
  }

  @override
  Widget build(BuildContext context) {
    final pingIcon = _scanning
        ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
        : (_pingOk == null
            ? const Icon(Icons.cloud_queue, color: Colors.grey)
            : (_pingOk! ? const Icon(Icons.check_circle, color: Colors.green) : const Icon(Icons.cancel, color: Colors.red)));
    return Scaffold(
      appBar: AppBar(
        title: const Text('AiCam — SAM 2 Live'),
        actions: [
          IconButton(
            tooltip: 'Rescan LAN',
            onPressed: _scanning ? null : _autoDiscover,
            icon: const Icon(Icons.radar),
          ),
          Padding(padding: const EdgeInsets.symmetric(horizontal: 8), child: Center(child: pingIcon)),
        ],
      ),
      body: GestureDetector(
        behavior: HitTestBehavior.translucent,
        onTap: () => FocusScope.of(context).unfocus(),
        child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.all(8),
            child: Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _backendCtl,
                    onChanged: _onBackendChanged,
                    decoration: const InputDecoration(labelText: 'Backend', isDense: true, border: OutlineInputBorder()),
                  ),
                ),
                const SizedBox(width: 8),
                FilledButton.tonal(
                  onPressed: _busy ? null : _testBackend,
                  child: const Text('Test'),
                ),
              ],
            ),
          ),
          Expanded(
            child: _camReady
                ? Stack(
                    fit: StackFit.expand,
                    children: [
                      CameraPreview(_cam!),
                      if (_overlay != null)
                        Positioned.fill(
                          child: IgnorePointer(
                            child: Opacity(
                              opacity: 0.55,
                              child: Image.memory(_overlay!, fit: BoxFit.cover, gaplessPlayback: true),
                            ),
                          ),
                        ),
                      Positioned(
                        left: 8,
                        top: 8,
                        child: Container(
                          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                          color: Colors.black54,
                          child: Text(
                            'sent:$_framesSent  recv:$_framesRecv  ${_fps}fps  $_status',
                            style: const TextStyle(color: Colors.white, fontSize: 12),
                          ),
                        ),
                      ),
                      if (_lastSpoken.isNotEmpty)
                        Positioned(
                          left: 8,
                          right: 8,
                          bottom: 8,
                          child: Container(
                            padding: const EdgeInsets.all(8),
                            decoration: BoxDecoration(
                              color: Colors.black.withValues(alpha: 0.6),
                              borderRadius: BorderRadius.circular(6),
                            ),
                            child: Row(
                              children: [
                                const Icon(Icons.volume_up, color: Colors.cyanAccent, size: 18),
                                const SizedBox(width: 8),
                                Expanded(
                                  child: Text(
                                    _lastSpoken,
                                    style: const TextStyle(color: Colors.white, fontSize: 13),
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ),
                    ],
                  )
                : Center(child: Text(_status.isEmpty ? 'Initializing camera…' : _status)),
          ),
          Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              children: [
                Row(
                  children: [
                    Expanded(
                      child: TextField(
                        controller: _chunkSecondsCtl,
                        keyboardType: TextInputType.number,
                        enabled: !_chunking,
                        decoration: const InputDecoration(labelText: 'Chunk sec', isDense: true, border: OutlineInputBorder()),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: TextField(
                        controller: _sampleFpsCtl,
                        keyboardType: const TextInputType.numberWithOptions(decimal: true),
                        enabled: !_chunking,
                        decoration: const InputDecoration(labelText: 'Sample FPS', isDense: true, border: OutlineInputBorder()),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                SizedBox(
                  width: double.infinity,
                  height: 52,
                  child: FilledButton.icon(
                    onPressed: _camReady ? _toggleChunking : null,
                    icon: Icon(_chunking ? Icons.stop_circle : Icons.videocam),
                    label: Text(_chunking ? 'Stop Real AI Cam' : 'Start Real AI Cam'),
                    style: FilledButton.styleFrom(
                      backgroundColor: _chunking ? Colors.red : Colors.green,
                    ),
                  ),
                ),
                if (_chunkStatus.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 6),
                    child: Align(
                      alignment: Alignment.centerLeft,
                      child: Text(_chunkStatus, style: const TextStyle(fontSize: 12, color: Colors.white70)),
                    ),
                  ),
                const SizedBox(height: 8),
                SizedBox(
                  width: double.infinity,
                  height: 52,
                  child: FilledButton.icon(
                    onPressed: (_camReady && !_chunking) ? _toggleStream : null,
                    icon: Icon(_streaming ? Icons.stop : Icons.play_arrow),
                    label: Text(_streaming ? 'Stop' : 'Start Live Segmentation'),
                    style: FilledButton.styleFrom(
                      backgroundColor: _streaming ? Colors.red : null,
                    ),
                  ),
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
