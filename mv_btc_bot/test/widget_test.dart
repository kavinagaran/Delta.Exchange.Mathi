import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:mv_btc_bot/main.dart';

void main() {
  testWidgets('App builds', (WidgetTester tester) async {
    await tester.pumpWidget(const MathiBotApp());
    expect(find.byType(MathiBotApp), findsOneWidget);
  });

  testWidgets('all app tabs fit a narrow Android screen', (
    WidgetTester tester,
  ) async {
    tester.view.physicalSize = const Size(360, 800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      MaterialApp(
        theme: buildAppTheme(blue: false),
        home: Scaffold(
          bottomNavigationBar: NavigationBar(
            destinations: [
              for (final page in appPages)
                NavigationDestination(
                  icon: Icon(page.icon),
                  label: page.navLabel,
                ),
            ],
          ),
        ),
      ),
    );

    expect(tester.takeException(), isNull);
    expect(find.text('Trend'), findsOneWidget);
    expect(find.text('Dry Run'), findsOneWidget);
  });

  test('all dashboard pages except Logs are exposed as tabs', () {
    expect(
      appPages.map((page) => page.label),
      equals([
        'Nithi Bot',
        'Trend Engine',
        'Trades & P&L',
        'Dry Run',
        'Positions',
        'Bot Config',
        'API Accounts',
      ]),
    );
    expect(
      appPages.map((page) => page.path),
      containsAllInOrder(['/', '/trend-engine', '/trades', '/dry-run']),
    );
    expect(appPages.any((page) => page.label == 'Logs'), isFalse);
  });

  test('native Red and Blue themes mirror the dashboard palette', () {
    expect(kRedBackground, const Color(0xFF0D0608));
    expect(kRedAccent, const Color(0xFFFF2F4B));
    expect(kBlueBackground, const Color(0xFF030914));
    expect(kBlueAccent, const Color(0xFF39A7FF));
    expect(buildAppTheme(blue: false).brightness, Brightness.dark);
    expect(buildAppTheme(blue: true).brightness, Brightness.dark);
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

  test('APK release refreshes cached web assets for Red/Blue Trend tabs', () {
    expect(kWebAssetRevision, '3.3.0+6-red-blue-trend-tabs');
  });
}
