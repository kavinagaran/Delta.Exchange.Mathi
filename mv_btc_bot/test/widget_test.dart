import 'package:flutter_test/flutter_test.dart';

import 'package:mv_btc_bot/main.dart';

void main() {
  testWidgets('App builds', (WidgetTester tester) async {
    await tester.pumpWidget(const MathiBotApp());
    expect(find.byType(MathiBotApp), findsOneWidget);
  });
}
