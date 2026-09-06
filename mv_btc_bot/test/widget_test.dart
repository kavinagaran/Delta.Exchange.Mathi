import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:mv_btc_bot/main.dart';
import 'package:mv_btc_bot/api/client.dart';
import 'package:mv_btc_bot/screens/cockpit_screen.dart';
import 'package:mv_btc_bot/screens/performance_screen.dart';
import 'package:mv_btc_bot/screens/today_screen.dart';
import 'package:mv_btc_bot/screens/trend_engine_screen.dart';
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

  testWidgets('Today shows one Exit action and every trade today', (
    WidgetTester tester,
  ) async {
    double? observedBtcPrice;
    tester.view.physicalSize = const Size(390, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      MaterialApp(
        theme: buildAppTheme(blue: true),
        // Scaffold, not TodayScreen directly, as `home` -- the real app
        // always hosts a tab body inside a Scaffold, and Material widgets
        // like the Cockpit's bot-toggle Switch need that Material ancestor
        // to find (Switch does not self-wrap in one the way buttons do).
        home: Scaffold(
          body: TodayScreen(
            api: _TodayApi(),
            onUnauthorised: () {},
            onBtcPrice: (price) => observedBtcPrice = price,
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    await expectLater(
      find.byType(TodayScreen),
      matchesGoldenFile('goldens/today_compact.png'),
    );

    expect(find.text('2 trades'), findsOneWidget);
    expect(observedBtcPrice, 64763);
    expect(find.text('+42.4'), findsOneWidget);
    expect(find.text('+46.8'), findsOneWidget);
    expect(find.text('BUY 2-STEP ITM CE'), findsOneWidget);
    expect(find.text('ENTRY READY'), findsNothing);
    expect(find.text('SIGNAL CONSUMED'), findsNothing);
    expect(find.text('P-BTC-63000-020826'), findsOneWidget);
    expect(find.text(r'-$53.30 (-11.22%)', findRichText: true), findsOneWidget);
    expect(find.text(r'-$65.90'), findsWidgets);
    expect(find.textContaining('7:41 AM IST'), findsOneWidget);
    // scrollUntilVisible stops as soon as any part of the target overlaps
    // the viewport, which used to be enough when this row sat near the top
    // of the list, so the row can land only partially onscreen (its centre --
    // what tap() targets -- still below the fold). ensureVisible aligns it
    // fully into view instead.
    await tester.scrollUntilVisible(
      find.text('P-BTC-63000-020826'),
      300,
      scrollable: find.byType(Scrollable).first,
    );
    await tester.ensureVisible(find.text('P-BTC-63000-020826'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('P-BTC-63000-020826'));
    await tester.pumpAndSettle();
    expect(find.textContaining('8:01 AM IST'), findsOneWidget);
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
            ],
          ),
        ),
      ),
    );

    expect(tester.takeException(), isNull);
    expect(find.text('Trend'), findsOneWidget);
    expect(find.text('Cockpit'), findsOneWidget);
    expect(find.text('Dry Run'), findsOneWidget);
    expect(find.text('Config'), findsOneWidget);
    // The /trades tab is labelled by its route, not by the page title — the
    // full 'Performance' does not fit the compact phone bar at 360dp.
    expect(find.text('Trades'), findsOneWidget);
    expect(find.text('Performance'), findsNothing);
  });

  testWidgets('Performance uses complete Delta trade records and net P&L', (
    WidgetTester tester,
  ) async {
    tester.view.physicalSize = const Size(390, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      MaterialApp(
        theme: buildAppTheme(blue: true),
        home: PerformanceScreen(api: _PerformanceApi(), onUnauthorised: () {}),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text(r'+$50.00'), findsWidgets);
    expect(find.text('50.0%'), findsOneWidget);
    expect(find.text('1 : 2.25'), findsOneWidget);
    expect(find.text(r'+$90.00'), findsWidgets);
    expect(find.text(r'-$40.00'), findsWidgets);
    expect(find.text(r'$22.00'), findsOneWidget);
    expect(find.text('3 trade cycles'), findsOneWidget);
  });

  testWidgets(
    'Cockpit gates strategies by setup and submits selected SELL PE',
    (WidgetTester tester) async {
      tester.view.physicalSize = const Size(390, 1200);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final api = _CockpitApi();

      await tester.pumpWidget(
        MaterialApp(
          theme: buildAppTheme(blue: true),
          home: Scaffold(
            body: CockpitScreen(api: api, onUnauthorised: () {}),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('LIVE COCKPIT'), findsOneWidget);
      expect(find.text('Order mode'), findsNothing);
      expect(find.text('Bullish trend score'), findsOneWidget);
      expect(find.text('ELIGIBLE'), findsOneWidget);

      await tester.tap(find.text('Bullish trend score'));
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
        find.text('ATM Put'),
        300,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.ensureVisible(find.text('ATM Put'));
      await tester.pumpAndSettle();
      expect(find.text('Protected premium-selling strategies'), findsOneWidget);
      await tester.tap(find.text('ATM Put'));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('Preview Order'));
      await tester.tap(find.text('Preview Order'));
      await tester.pump(const Duration(milliseconds: 500));

      expect(find.text('Confirm LIVE order'), findsOneWidget);
      expect(find.text('P-BTC-64000-140826'), findsOneWidget);
      expect(api.previewAction, 'sell_pe');
      expect(api.previewSetup, 'trend_bullish');

      await tester.tap(find.text('Place LIVE Order'));
      await tester.pump(const Duration(milliseconds: 500));
      expect(api.enterAction, 'sell_pe');
      expect(api.enterSetup, 'trend_bullish');

      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets('Cockpit opens DRY RUN simulations while the bot remains on', (
    WidgetTester tester,
  ) async {
    tester.view.physicalSize = const Size(390, 1200);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final api = _CockpitApi(dryRun: true);

    await tester.pumpWidget(
      MaterialApp(
        theme: buildAppTheme(blue: true),
        home: Scaffold(
          body: CockpitScreen(api: api, onUnauthorised: () {}),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('DRY RUN COCKPIT'), findsOneWidget);
    expect(find.text('Order mode'), findsNothing);
    expect(find.text('DRY RUN READY'), findsOneWidget);
    expect(find.text('Orders go to the DRY RUN dashboard'), findsOneWidget);
    await tester.tap(find.text('Bullish trend score'));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.text('2-Step ITM Call'),
      300,
      scrollable: find.byType(Scrollable).first,
    );
    await tester.ensureVisible(find.text('2-Step ITM Call'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('2-Step ITM Call'));
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Preview Order'));
    await tester.tap(find.text('Preview Order'));
    await tester.pump(const Duration(milliseconds: 500));

    expect(find.text('Confirm DRY RUN trade'), findsOneWidget);
    expect(find.text('Open DRY RUN Trade'), findsOneWidget);
    expect(
      find.text(
        'This opens a DRY RUN simulation. No order is sent to Delta Exchange.',
      ),
      findsOneWidget,
    );
    await tester.tap(find.text('Open DRY RUN Trade'));
    await tester.pump(const Duration(milliseconds: 500));
    expect(api.enterAction, 'buy_ce');
    expect(api.enterSetup, 'trend_bullish');

    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('Trend preview reads live_score and chart supports touch zoom', (
    WidgetTester tester,
  ) async {
    tester.view.physicalSize = const Size(390, 1200);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      MaterialApp(
        theme: buildAppTheme(blue: true),
        home: TrendEngineScreen(api: _TrendApi(), onUnauthorised: () {}),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('-53.0'), findsOneWidget);
    expect(find.text('-57.6'), findsOneWidget);
    expect(find.text('BUY 2-STEP ITM PE'), findsOneWidget);
    expect(find.byKey(const ValueKey('committed-score-chart')), findsOneWidget);
    final chart = tester.widget<InteractiveViewer>(
      find.byKey(const ValueKey('committed-score-chart')),
    );
    expect(chart.panEnabled, isTrue);
    expect(chart.scaleEnabled, isTrue);
    expect(chart.maxScale, 8);

    await tester.pumpWidget(const SizedBox.shrink());
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
        'Cockpit',
        'Performance',
        'Exposure',
        'Bot Config',
        'API Accounts',
        'Logs',
        'Dry Run',
        'Trend Engine',
      ]),
    );
    expect(
      appPages.map((page) => page.path),
      containsAllInOrder([
        '/',
        '/cockpit',
        '/trades',
        '/dry-run',
        '/trend-engine',
      ]),
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
          body: Text('BTC BOT', style: neonBrandTextStyle(fontSize: 16)),
        ),
      ),
    );

    final style = tester.widget<Text>(find.text('BTC BOT')).style!;
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

  testWidgets('TSL telemetry reads the watchdog fields the web page reads', (
    WidgetTester tester,
  ) async {
    // The state from the live app: armed, but on the watchdog path, so every
    // `stream_*` field is null. Reading only those made the line contradict
    // its own pill -- "TSL ARMED" beside "not armed / peak pending".
    tester.view.physicalSize = const Size(390, 1400);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      MaterialApp(
        theme: buildAppTheme(blue: true),
        home: Scaffold(
          body: TodayScreen(
            api: _WatchdogProtectionApi(),
            onUnauthorised: () {},
            onBtcPrice: (_) {},
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('TSL ARMED'), findsOneWidget);
    expect(
      find.text(
        'TSL Armed · floor \$360.36 · peak \$412.90 · EXCHANGE PROTECTED',
      ),
      findsOneWidget,
    );
  });
}

class _TodayApi extends DashboardApi {
  _TodayApi() : super(baseUrl: 'https://example.invalid', sessionCookie: null);

  @override
  Future<ApiResult<Map<String, dynamic>>> status() async =>
      const ApiResult.ok(<String, dynamic>{'btc_futures_price': 64763.0});

  @override
  Future<ApiResult<Map<String, dynamic>>> engineSnapshot() async =>
      const ApiResult.ok(<String, dynamic>{
        'data_quality': 'OK',
        'zone': 'CE_2_ITM',
        'trend_score': 42.4,
        'trigger_adx': 28.4,
        'zone_action_allowed': true,
      });

  @override
  Future<ApiResult<Map<String, dynamic>>> scoreAutoStatus() async =>
      const ApiResult.ok(<String, dynamic>{});

  @override
  Future<ApiResult<Map<String, dynamic>>> engineLive() async =>
      const ApiResult.ok(<String, dynamic>{'live_score': 46.8});

  @override
  Future<ApiResult<Map<String, dynamic>>> protectionStatus() async =>
      const ApiResult.ok(<String, dynamic>{});

  @override
  Stream<ApiResult<Map<String, dynamic>>> protectionStream() =>
      const Stream.empty();

  @override
  Future<ApiResult<List<dynamic>>> todayTrades() async => ApiResult.ok([
    <String, dynamic>{
      '_live': true,
      'status': 'OPEN',
      'symbol': 'C-BTC-64000-020826',
      'side': 'long',
      'lots': 1000,
      'entry_mark': 475.0,
      'contract_value': 0.001,
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

class _CockpitApi extends DashboardApi {
  _CockpitApi({this.dryRun = false})
    : super(baseUrl: 'https://example.invalid', sessionCookie: null);

  final bool dryRun;

  String? previewAction;
  String? previewSetup;
  String? enterAction;
  String? enterSetup;

  @override
  Future<ApiResult<Map<String, dynamic>>> cockpitSetups() async =>
      const ApiResult.ok(<String, dynamic>{
        'data_quality': 'OK',
        'score': 52.0,
        'adx': 31.0,
        'regime': 'TREND_UP',
        'setups': <String, dynamic>{
          'trend_bullish': <String, dynamic>{
            'eligible': true,
            'actions': <dynamic>['buy_ce', 'sell_pe'],
            'detail': 'Score > +40',
          },
          'trend_bearish': <String, dynamic>{
            'eligible': false,
            'actions': <dynamic>['buy_pe', 'sell_ce'],
          },
        },
      });

  @override
  Future<ApiResult<Map<String, dynamic>>> scoreAutoStatus() async =>
      ApiResult.ok(<String, dynamic>{
        'mode': dryRun ? 'dry_run' : 'live',
        'account_live': !dryRun,
        'account_trading_mode': dryRun ? 'DRY RUN' : 'LIVE',
        'position_status': 'NONE',
        'setup_lock': const <String, dynamic>{
          'active': true,
          'zone': 'CE_2_ITM',
        },
      });

  @override
  Future<ApiResult<List<dynamic>>> todayTrades() async =>
      const ApiResult.ok(<dynamic>[]);

  @override
  Future<ApiResult<Map<String, dynamic>>> cockpitPreview(
    String action,
    String setup,
  ) async {
    previewAction = action;
    previewSetup = setup;
    return ApiResult.ok(<String, dynamic>{
      'side': 'short',
      'instrument_kind': 'BTC_OPTION',
      'symbol': 'P-BTC-64000-140826',
      'strike': 64000,
      'entry_price': 325.0,
      'lots': 500,
      'dry_run': dryRun,
      'execution_mode': dryRun ? 'dry_run' : 'live',
    });
  }

  @override
  Future<ApiResult<Map<String, dynamic>>> cockpitEnter(
    String action,
    String setup,
  ) async {
    enterAction = action;
    enterSetup = setup;
    return ApiResult.ok(<String, dynamic>{
      'dry_run': dryRun,
      'state': const <String, dynamic>{
        'symbol': 'P-BTC-64000-140826',
        'lots': 500,
      },
    });
  }
}

class _PerformanceApi extends DashboardApi {
  _PerformanceApi()
    : super(baseUrl: 'https://example.invalid', sessionCookie: null);

  @override
  Future<ApiResult<List<dynamic>>> performanceTrades() async => ApiResult.ok([
    <String, dynamic>{
      'status': 'CLOSED',
      'symbol': 'C-BTC-63000-030826',
      'side': 'long',
      'lots': 1000,
      'gross_pnl_usd': 100.0,
      'net_pnl_usd': 90.0,
      'exit_at_utc': '2026-08-03T01:00:00Z',
      'fees': [
        <String, dynamic>{'asset': 'USD', 'amount': 10.0},
      ],
    },
    <String, dynamic>{
      'status': 'CLOSED',
      'symbol': 'P-BTC-63000-030826',
      'side': 'long',
      'lots': 1000,
      'gross_pnl_usd': -30.0,
      'net_pnl_usd': -40.0,
      'exit_at_utc': '2026-08-03T02:00:00Z',
      'fees': [
        <String, dynamic>{'asset': 'USD', 'amount': 10.0},
      ],
    },
    <String, dynamic>{
      'status': 'OPEN',
      'symbol': 'MV-BTC-63000-030826',
      'side': 'short',
      'lots': 1000,
      'fees': [
        <String, dynamic>{'asset': 'USD', 'amount': 2.0},
      ],
    },
  ]);
}

class _TrendApi extends DashboardApi {
  _TrendApi() : super(baseUrl: 'https://example.invalid', sessionCookie: null);

  @override
  Future<ApiResult<Map<String, dynamic>>> engineSnapshot() async =>
      const ApiResult.ok(<String, dynamic>{
        'data_quality': 'OK',
        'trend_score': -57.6,
        'trigger_adx': 25.0,
        'zone': 'PE_2_ITM',
        'zone_action_allowed': true,
        'confidence': .84,
        'regime': 'TREND_DOWN',
        'components': <dynamic>[],
        'timeframes': <dynamic>[],
        'gates': <dynamic>[],
        'reason_codes': <dynamic>[],
      });

  @override
  Future<ApiResult<Map<String, dynamic>>> engineLive() async =>
      const ApiResult.ok(<String, dynamic>{
        'available': true,
        'live_score': -53.0,
      });

  @override
  Future<ApiResult<Map<String, dynamic>>> engineStatus() async =>
      const ApiResult.ok(<String, dynamic>{'available': true});

  @override
  Future<ApiResult<Map<String, dynamic>>> decisionHistory() async =>
      const ApiResult.ok(<String, dynamic>{
        'decisions': <dynamic>[
          <String, dynamic>{'committed_score': -40.0},
          <String, dynamic>{'committed_score': -57.6},
        ],
        'trade_markers': <dynamic>[],
      });
}

/// Armed trailing stop on the watchdog path: no `stream_*` telemetry at all,
/// the floor carried top-level and the peak only inside `health`, exactly as
/// `_tp_monitor_payload` emits it when no live stream is attached.
class _WatchdogProtectionApi extends _TodayApi {
  @override
  Future<ApiResult<Map<String, dynamic>>> protectionStatus() async =>
      const ApiResult.ok(<String, dynamic>{
        'trend': <String, dynamic>{
          'running': true,
          'streaming': false,
          'poll_secs': 10,
          'tsl_armed': true,
          'tsl_floor': 360.36,
          'target_pnl': 1204.0,
          'sl_pnl': 361.2,
          'tsl_arm_pnl': 361.2,
          'tsl_trail_pnl': 360.36,
          'tsl_lock_min_pnl': 0,
          'coverage_status': 'exchange_protected',
          'health': <String, dynamic>{'peak_pnl': 412.9},
        },
      });
}
