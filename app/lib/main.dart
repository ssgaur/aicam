import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'controller.dart';
import 'screens/clips_screen.dart';
import 'screens/dashboard_screen.dart';
import 'screens/settings_screen.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final prefs = await SharedPreferences.getInstance();
  runApp(AiCamMonitorApp(controller: AppController(prefs)));
}

class AiCamMonitorApp extends StatefulWidget {
  final AppController controller;
  const AiCamMonitorApp({super.key, required this.controller});

  @override
  State<AiCamMonitorApp> createState() => _AiCamMonitorAppState();
}

class _AiCamMonitorAppState extends State<AiCamMonitorApp> {
  @override
  void initState() {
    super.initState();
    widget.controller.startPolling();
  }

  @override
  void dispose() {
    widget.controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'AiCam Monitor',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF1565C0),
          brightness: Brightness.dark,
        ),
        useMaterial3: true,
      ),
      home: HomeShell(controller: widget.controller),
    );
  }
}

class HomeShell extends StatefulWidget {
  final AppController controller;
  const HomeShell({super.key, required this.controller});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _index = 0;

  @override
  Widget build(BuildContext context) {
    final c = widget.controller;
    final pages = [
      DashboardScreen(controller: c),
      ClipsScreen(controller: c),
      SettingsScreen(controller: c),
    ];
    return Scaffold(
      body: SafeArea(child: pages[_index]),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (i) => setState(() => _index = i),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.dashboard_outlined),
            selectedIcon: Icon(Icons.dashboard),
            label: 'Live',
          ),
          NavigationDestination(
            icon: Icon(Icons.video_library_outlined),
            selectedIcon: Icon(Icons.video_library),
            label: 'Clips',
          ),
          NavigationDestination(
            icon: Icon(Icons.settings_outlined),
            selectedIcon: Icon(Icons.settings),
            label: 'Settings',
          ),
        ],
      ),
    );
  }
}
