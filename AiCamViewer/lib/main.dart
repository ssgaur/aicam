import 'package:flutter/material.dart';
import 'screens/viewer_screen.dart';
import 'services/api.dart';

void main() {
  runApp(const AiCamApp());
}

class AiCamApp extends StatefulWidget {
  const AiCamApp({super.key});

  @override
  State<AiCamApp> createState() => _AiCamAppState();
}

class _AiCamAppState extends State<AiCamApp> {
  // Default to Azure VM; user can change in settings
  final api = AicamApi(baseUrl: 'https://20.197.31.88:8100');
  bool _connected = false;
  bool _checking = true;

  @override
  void initState() {
    super.initState();
    _checkConnection();
  }

  Future<void> _checkConnection() async {
    final ok = await api.healthCheck();
    setState(() {
      _connected = ok;
      _checking = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'AiCam Viewer',
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark().copyWith(
        scaffoldBackgroundColor: const Color(0xFF0A0C12),
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF4F8CFF),
          brightness: Brightness.dark,
        ),
      ),
      home: _checking
          ? const Scaffold(
              backgroundColor: Color(0xFF0A0C12),
              body: Center(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.videocam, color: Color(0xFF4F8CFF), size: 48),
                    SizedBox(height: 16),
                    CircularProgressIndicator(color: Color(0xFF4F8CFF)),
                    SizedBox(height: 12),
                    Text('Connecting to AiCam...',
                        style: TextStyle(color: Color(0xFF6E7490))),
                  ],
                ),
              ),
            )
          : _connected
              ? ViewerScreen(api: api)
              : _buildConnectionError(),
    );
  }

  Widget _buildConnectionError() {
    return Scaffold(
      backgroundColor: const Color(0xFF0A0C12),
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.cloud_off, color: Color(0xFFF87171), size: 48),
            const SizedBox(height: 16),
            const Text('Cannot reach AiCam server',
                style: TextStyle(color: Colors.white, fontSize: 16)),
            const SizedBox(height: 8),
            Text(api.baseUrl,
                style: const TextStyle(color: Color(0xFF6E7490), fontSize: 12)),
            const SizedBox(height: 20),
            ElevatedButton.icon(
              onPressed: () {
                setState(() => _checking = true);
                _checkConnection();
              },
              icon: const Icon(Icons.refresh),
              label: const Text('Retry'),
            ),
          ],
        ),
      ),
    );
  }
}
