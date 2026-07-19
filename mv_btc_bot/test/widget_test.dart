import 'package:flutter_test/flutter_test.dart';

import 'package:mv_btc_bot/main.dart';

void main() {
  testWidgets('App builds', (WidgetTester tester) async {
    await tester.pumpWidget(const MathiBotApp());
    expect(find.byType(MathiBotApp), findsOneWidget);
  });

  test('all dashboard pages except Logs are exposed as tabs', () {
    expect(
      appPages.map((page) => page.label),
      equals([
        'Nithi Bot',
        'Trades & P&L',
        'Dry Run',
        'Positions',
        'Bot Config',
        'API Accounts',
      ]),
    );
    expect(appPages.any((page) => page.label == 'Logs'), isFalse);
  });

  test('Flask session cookie is extracted for the embedded dashboard', () {
    expect(
      SessionService.sessionCookieFromHeader(
        'session=eyJ1c2VyIjoibWF0aGkifQ.signature; HttpOnly; Path=/',
      ),
      'eyJ1c2VyIjoibWF0aGkifQ.signature',
    );
    expect(SessionService.sessionCookieFromHeader('other=value'), isNull);
  });

  test('APK release refreshes cached web assets for the protection monitor', () {
    expect(kWebAssetRevision, '3.2.0+5-dry-protection-monitor');
  });
}
