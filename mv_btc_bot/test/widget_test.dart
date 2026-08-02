import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:mv_btc_bot/main.dart';
import 'package:mv_btc_bot/api/client.dart';
import 'package:mv_btc_bot/screens/today_screen.dart';
import 'package:mv_btc_bot/widgets/kit.dart';

void main() {
  testWidgets('App builds', (WidgetTester tester) async {
    await tester.pumpWidget(const MathiBotApp());
    expect(find.byType(MathiBotApp), findsOneWidget);
  });

  testWidgets('cards render inside an unbounded scrolling page', (
    WidgetTester tester,
  ) async {
    tester.view.physicalSize = const Size(360, 800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      MaterialApp(
        theme: buildAppTheme(blue: true),
        home: Scaffold(
          body: ListView(
            children: const [
              AppCard(
                kicker: 'Trend engine',
                title: 'Configuration',
                accent: kPositive,
                child: Text('Visible content'),
              ),
            ],
          ),
        ),
      ),
    );

    expect(tester.takeException(), isNull);
    expect(find.text('Configuration'), findsOneWidget);
    expect(find.text('Visible content'), findsOneWidget);
  });

  testWidgets('Today shows one Exit action and a detailed latest trade', (
    WidgetTester tester,
  ) async {
    tester.view.physicalSize = const Size(390, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      MaterialApp(
        theme: buildAppTheme(blue: true),
        home: TodayScreen(api: _TodayApi(), onUnauthorised: () {}),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Latest Trade'), findsOneWidget);
    expect(find.text('P-BTC-63000-020826'), findsOneWidget);
    expect(find.text(r'-$65.90'), findsOneWidget);
    expect(find.text('7:41 AM IST'), findsOneWidget);
    expect(find.text('8:01 AM IST'), findsOneWidget);
    expect(find.text('Exit'), findsOneWidget);
    expect(find.text('Close'), findsNothing);
    expect(find.text('Protection'), findsNothing);
    expect(find.text('Payoff'), findsNothing);

    // Dispose the screen's polling timer before the test ends.
    await tester.pumpWidget(const SizedBox.shrink());
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
              for (final index in primaryPageIndexes)
                NavigationDestination(
                  icon: Icon(appPages[index].icon),
                  label: appPages[index].navLabel,
                ),
              const NavigationDestination(
                icon: Icon(Icons.grid_view_rounded),
                label: 'More',
              ),
            ],
          ),
        ),
      ),
    );

    expect(tester.takeException(), isNull);
    expect(find.text('Trend'), findsOneWidget);
    expect(find.text('Paper'), findsOneWidget);
    expect(find.text('More'), findsOneWidget);
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

  test('all dashboard pages are exposed as native destinations', () {
    expect(
      appPages.map((page) => page.label),
      equals([
        'Today',
        'Performance',
        'Paper',
        'Exposure',
        'Bot Config',
        'API Accounts',
        'Logs',
        'Trend Engine',
      ]),
    );
    expect(
      appPages.map((page) => page.path),
      containsAllInOrder(['/', '/trades', '/dry-run', '/trend-engine']),
    );
    expect(appPages.any((page) => page.label == 'Logs'), isTrue);
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

  test(
    'APK release ships a versioned web-asset revision for cache busting',
    () {
      // Asserting the format rather than a literal so the test does not need
      // editing on every bump — the invariant is that the cache-bust token
      // exists and follows the "<major>.<minor>.<patch>+<build>-<slug>" shape.
      expect(kWebAssetRevision, isNotEmpty);
      expect(kWebAssetRevision, matches(RegExp(r'^\d+\.\d+\.\d+\+\d+-')));
    },
  );
}

class _TodayApi extends DashboardApi {
  _TodayApi() : super(baseUrl: 'https://example.invalid', sessionCookie: null);

  @override
  Future<ApiResult<Map<String, dynamic>>> status() async =>
      const ApiResult.ok(<String, dynamic>{});

  @override
  Future<ApiResult<List<dynamic>>> todayTrades() async => ApiResult.ok([
    <String, dynamic>{
      '_live': true,
      'status': 'OPEN',
      'symbol': 'C-BTC-64000-020826',
      'side': 'long',
      'lots': 1000,
      'entry_mark': 475.0,
      'current_mark': 421.7,
      'live_pnl': -53.3,
    },
    <String, dynamic>{
      'status': 'CLOSED',
      'symbol': 'P-BTC-63000-020826',
      'side': 'long',
      'lots': 1000,
      'entry_mark': 475.0,
      'exit_mark': 409.1,
      'pnl_usd': -65.9,
      'entry_date': '2026-08-02',
      'entry_time_utc': '02:11:00',
      'exit_date': '2026-08-02',
      'exit_time_utc': '02:31:00',
      'exit_trigger': 'trailing_stop',
    },
  ]);
}
