import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api.dart';

const _kBackendUrl = 'backend_url';
const _kWindow = 'window';
const _kRefreshSec = 'refresh_sec';

const defaultBackendUrl = 'http://192.168.1.72:8100';

/// Selectable time windows for the summary endpoint.
const windowOptions = <String>['10m', '1h', '6h', '24h'];
const refreshOptions = <int>[3, 5, 10, 30];

/// Holds settings + the latest fetched data and drives periodic polling.
class AppController extends ChangeNotifier {
  AppController(this._prefs) {
    _backendUrl = _prefs.getString(_kBackendUrl) ?? defaultBackendUrl;
    _window = _prefs.getString(_kWindow) ?? '1h';
    _refreshSec = _prefs.getInt(_kRefreshSec) ?? 5;
    _client = ApiClient(_backendUrl);
  }

  final SharedPreferences _prefs;
  late ApiClient _client;
  Timer? _timer;

  late String _backendUrl;
  late String _window;
  late int _refreshSec;

  LiveStatus? status;
  WindowSummary? summary;
  bool loading = false;
  bool connected = false;
  String? error;
  DateTime? lastUpdated;

  String get backendUrl => _backendUrl;
  String get window => _window;
  int get refreshSec => _refreshSec;

  String get backendHost {
    final u = Uri.tryParse(_backendUrl);
    if (u == null || u.host.isEmpty) return _backendUrl;
    return u.port > 0 ? '${u.host}:${u.port}' : u.host;
  }

  void startPolling() {
    _timer?.cancel();
    refresh();
    _timer = Timer.periodic(Duration(seconds: _refreshSec), (_) => refresh());
  }

  void stopPolling() {
    _timer?.cancel();
    _timer = null;
  }

  Future<void> refresh() async {
    if (loading) return;
    loading = true;
    notifyListeners();
    try {
      final results = await Future.wait([
        _client.fetchStatus(),
        _safeSummary(),
      ]);
      status = results[0] as LiveStatus;
      summary = results[1] as WindowSummary?;
      connected = true;
      error = null;
      lastUpdated = DateTime.now();
    } catch (e) {
      connected = false;
      error = e.toString();
    } finally {
      loading = false;
      notifyListeners();
    }
  }

  /// Summary route may be missing on older backends; degrade gracefully.
  Future<WindowSummary?> _safeSummary() async {
    try {
      return await _client.fetchSummary(_window);
    } catch (_) {
      return null;
    }
  }

  Future<void> setBackendUrl(String url) async {
    _backendUrl = url.trim();
    _client = ApiClient(_backendUrl);
    await _prefs.setString(_kBackendUrl, _backendUrl);
    notifyListeners();
    startPolling();
  }

  Future<bool> testConnection(String url) async {
    return ApiClient(url.trim()).ping();
  }

  Future<void> setWindow(String w) async {
    if (w == _window) return;
    _window = w;
    await _prefs.setString(_kWindow, w);
    notifyListeners();
    await refresh();
  }

  Future<void> setRefreshSec(int s) async {
    if (s == _refreshSec) return;
    _refreshSec = s;
    await _prefs.setInt(_kRefreshSec, s);
    notifyListeners();
    startPolling();
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }
}
