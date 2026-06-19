import 'package:flutter/material.dart';

/// Icon, color and friendly label for each detected object class.
class ClassStyle {
  final IconData icon;
  final Color color;
  final String label;
  const ClassStyle(this.icon, this.color, this.label);
}

const _styles = <String, ClassStyle>{
  'person': ClassStyle(Icons.directions_walk, Color(0xFF42A5F5), 'People'),
  'car': ClassStyle(Icons.directions_car, Color(0xFF66BB6A), 'Cars'),
  'motorcycle': ClassStyle(Icons.two_wheeler, Color(0xFFFFA726), 'Motorcycles'),
  'bus': ClassStyle(Icons.directions_bus, Color(0xFFAB47BC), 'Buses'),
  'truck': ClassStyle(Icons.local_shipping, Color(0xFFEF5350), 'Trucks'),
  'bicycle': ClassStyle(Icons.pedal_bike, Color(0xFF26C6DA), 'Bicycles'),
  'dog': ClassStyle(Icons.pets, Color(0xFF8D6E63), 'Dogs'),
};

ClassStyle styleFor(String cls) {
  return _styles[cls.toLowerCase()] ??
      ClassStyle(Icons.category, const Color(0xFF90A4AE), _titleCase(cls));
}

String _titleCase(String s) =>
    s.isEmpty ? s : s[0].toUpperCase() + s.substring(1);

/// Human friendly "x ago" for a timestamp.
String timeAgo(DateTime? t) {
  if (t == null) return '—';
  final d = DateTime.now().difference(t);
  if (d.inSeconds < 60) return '${d.inSeconds}s ago';
  if (d.inMinutes < 60) return '${d.inMinutes}m ago';
  if (d.inHours < 24) return '${d.inHours}h ago';
  return '${d.inDays}d ago';
}

String windowLabel(String w) {
  switch (w) {
    case '10m':
      return 'last 10 min';
    case '1h':
      return 'last hour';
    case '6h':
      return 'last 6 hours';
    case '24h':
      return 'last 24 hours';
    default:
      return w;
  }
}
