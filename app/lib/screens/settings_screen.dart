import 'package:flutter/material.dart';

import '../controller.dart';

class SettingsScreen extends StatefulWidget {
  final AppController controller;
  const SettingsScreen({super.key, required this.controller});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final TextEditingController _urlCtrl;
  String? _testResult;
  bool _testing = false;

  @override
  void initState() {
    super.initState();
    _urlCtrl = TextEditingController(text: widget.controller.backendUrl);
  }

  @override
  void dispose() {
    _urlCtrl.dispose();
    super.dispose();
  }

  Future<void> _test() async {
    setState(() {
      _testing = true;
      _testResult = null;
    });
    final ok = await widget.controller.testConnection(_urlCtrl.text);
    if (!mounted) return;
    setState(() {
      _testing = false;
      _testResult = ok ? 'Reachable ✓' : 'Not reachable ✗';
    });
  }

  Future<void> _save() async {
    await widget.controller.setBackendUrl(_urlCtrl.text);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Backend saved')),
    );
  }

  @override
  Widget build(BuildContext context) {
    final c = widget.controller;
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Text('Backend', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        TextField(
          controller: _urlCtrl,
          keyboardType: TextInputType.url,
          autocorrect: false,
          decoration: const InputDecoration(
            labelText: 'Backend URL',
            hintText: 'http://192.168.1.72:8100',
            border: OutlineInputBorder(),
            prefixIcon: Icon(Icons.dns),
          ),
        ),
        const SizedBox(height: 12),
        Row(
          children: [
            FilledButton.icon(
              onPressed: _save,
              icon: const Icon(Icons.save),
              label: const Text('Save'),
            ),
            const SizedBox(width: 12),
            OutlinedButton.icon(
              onPressed: _testing ? null : _test,
              icon: _testing
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.wifi_tethering),
              label: const Text('Test'),
            ),
            const SizedBox(width: 12),
            if (_testResult != null)
              Text(
                _testResult!,
                style: TextStyle(
                  color: _testResult!.contains('✓')
                      ? Colors.green
                      : Colors.red,
                  fontWeight: FontWeight.bold,
                ),
              ),
          ],
        ),
        const Divider(height: 40),
        Text('Auto-refresh', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        ListenableBuilder(
          listenable: c,
          builder: (context, _) => Wrap(
            spacing: 8,
            children: [
              for (final s in refreshOptions)
                ChoiceChip(
                  label: Text('${s}s'),
                  selected: c.refreshSec == s,
                  onSelected: (_) => c.setRefreshSec(s),
                ),
            ],
          ),
        ),
        const Divider(height: 40),
        const ListTile(
          contentPadding: EdgeInsets.zero,
          leading: Icon(Icons.info_outline),
          title: Text('AiCam Monitor'),
          subtitle: Text(
            'Companion viewer for the AiCam YOLO pipeline. '
            'Live counts, charts and recent clips from the Mac backend.',
          ),
        ),
      ],
    );
  }
}
