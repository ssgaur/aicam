import 'package:flutter_test/flutter_test.dart';
import 'package:aicam_viewer/main.dart';

void main() {
  testWidgets('App launches without crashing', (WidgetTester tester) async {
    await tester.pumpWidget(const AiCamApp());
    expect(find.text('Connecting to AiCam...'), findsOneWidget);
  });
}
