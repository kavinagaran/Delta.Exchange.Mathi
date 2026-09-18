import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mv_btc_bot/api/client.dart';
import 'package:mv_btc_bot/screens/config_screen.dart';
import 'package:mv_btc_bot/main.dart' show AccountValuePill, BtcPricePill;

class ConfigApi extends DashboardApi {
  ConfigApi({this.mode = 'live', this.dryRun = false})
    : super(baseUrl: 'https://example.invalid', sessionCookie: 'test');
  final String mode;
  final bool dryRun;
  bool reject = false;
  Map<String, dynamic>? posted;
  @override
  Future<ApiResult<Map<String, dynamic>>> config() async => ApiResult.ok({
    'DRY_RUN': '$dryRun',
    'TREND_ENGINE_SCORE_AUTO_MODE': mode,
  });
  @override
  Future<ApiResult<Map<String, dynamic>>> tradingMode() async =>
      const ApiResult.ok({'mode_change_allowed': true});
  @override
  Future<ApiResult<Map<String, dynamic>>> scoreAutoStatus() async =>
      const ApiResult.ok({});
  @override
  Future<ApiResult<dynamic>> saveConfig(Map<String, dynamic> values) async {
    posted = values;
    return reject
        ? const ApiResult.failed('Position is open')
        : const ApiResult.ok({'ok': true});
  }
}

void main() {
  testWidgets('large BTC pill fits narrow toolbar with 24h change', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          appBar: AppBar(
            toolbarHeight: 60,
            leadingWidth: 57,
            leading: const Icon(Icons.currency_bitcoin),
            title: const Row(
              children: [
                Expanded(
                  child: BtcPricePill(
                    price: 78357,
                    direction: -1,
                    changePct: -0.60,
                    expanded: true,
                  ),
                ),
                SizedBox(width: 6),
                AccountValuePill(valueInr: 17125.8),
              ],
            ),
            actions: [
              IconButton(onPressed: () {}, icon: const Icon(Icons.more_vert)),
            ],
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text(r'BTC  $78,357'), findsOneWidget);
    expect(find.text('24h  -0.60%'), findsOneWidget);
    expect(find.text('₹17,126'), findsOneWidget);
    expect(
      tester.getSize(find.byType(BtcPricePill)).width,
      greaterThan(tester.getSize(find.byType(AccountValuePill)).width),
    );
    expect(tester.takeException(), isNull);
  });
  for (final rejected in [false, true]) {
    testWidgets('Bot OFF saves immediately; rejection=$rejected', (
      tester,
    ) async {
      final api = ConfigApi()..reject = rejected;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ConfigScreen(
              api: api,
              onUnauthorised: () {},
              displayName: 'Test',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      final toggle = find.byKey(const ValueKey('config-bot-toggle'));
      expect(tester.getTopLeft(toggle).dy, lessThan(120));
      await tester.tap(toggle);
      await tester.pumpAndSettle();
      expect(api.posted, {'TREND_ENGINE_SCORE_AUTO_MODE': 'disabled'});
      expect(tester.widget<SwitchListTile>(toggle).value, rejected);
      expect(tester.takeException(), isNull);
    });
  }
  testWidgets('Bot ON respects saved dry run account', (tester) async {
    final api = ConfigApi(mode: 'disabled', dryRun: true);
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ConfigScreen(
            api: api,
            onUnauthorised: () {},
            displayName: 'Test',
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('config-bot-toggle')));
    await tester.pumpAndSettle();
    expect(api.posted, {'TREND_ENGINE_SCORE_AUTO_MODE': 'dry_run'});
    expect(find.text('BOT ON'), findsOneWidget);
  });
}
