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
    expect(find.text('Paper'), findsOneWidget);
    // The /trades tab is labelled by its route, not by the page title — the
    // full 'Performance' does not fit a seven-tab bar at 360dp.
    expect(find.text('Trades'), findsOneWidget);
    expect(find.text('Performance'), findsNothing);
  });

  testWidgets('tab icons are neon green in both themes', (
    WidgetTester tester,
  ) async {
    for (final blue in [false, true]) {
      final iconTheme = buildAppTheme(blue: blue).navigationBarTheme.iconTheme!;
      final selected = iconTheme.resolve({WidgetState.selected})!;
      final idle = iconTheme.resolve(<WidgetState>{})!;

      expect(selected.color, kNeon);
      expect(idle.color, kNeon.withValues(alpha: .72));
      // Both states glow; selected simply carries the wider halos.
      expect(selected.shadows, kNeonIconGlowStrong);
      expect(idle.shadows, kNeonIconGlow);
    }
  });

  test('all dashboard pages except Logs are exposed as tabs', () {
    expect(
      appPages.map((page) => page.label),
      equals([
        'Nithi Bot',
        'Performance',
        'Paper',
        'Exposure',
        'Bot Config',
        'API Accounts',
        'Trend Engine',
      ]),
    );
    expect(
      appPages.map((page) => page.path),
      containsAllInOrder(['/', '/trades', '/dry-run', '/trend-engine']),
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

  test('the neon brand accent matches --neon in static/css/app.css', () {
    expect(kNeon, const Color(0xFF39FF14));
    expect(kNeonTitle, const Color(0xFFEAFFE4));
    expect(kNeonSubtle, const Color(0xFF6DFF4D));
    // The web keeps --neon in the base :root rather than in a theme block, so
    // neither native theme may pull the brand glow towards its own accent.
    expect(
      buildAppTheme(blue: false).navigationBarTheme.indicatorColor,
      buildAppTheme(blue: true).navigationBarTheme.indicatorColor,
    );
  });

  testWidgets('the brand title carries the neon glow', (
    WidgetTester tester,
  ) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Text('Nithi Bot', style: neonBrandTextStyle(fontSize: 16)),
        ),
      ),
    );

    final style = tester.widget<Text>(find.text('Nithi Bot')).style!;
    expect(style.color, kNeonTitle);
    expect(style.shadows, kNeonTextGlow);
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

  test('APK release ships a versioned web-asset revision for cache busting', () {
    // Asserting the format rather than a literal so the test does not need
    // editing on every bump — the invariant is that the cache-bust token
    // exists and follows the "<major>.<minor>.<patch>+<build>-<slug>" shape.
    expect(kWebAssetRevision, isNotEmpty);
    expect(kWebAssetRevision, matches(RegExp(r'^\d+\.\d+\.\d+\+\d+-')));
  });
}
