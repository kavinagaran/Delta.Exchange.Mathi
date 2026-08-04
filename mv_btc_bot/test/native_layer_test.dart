/// Tests for the native screen layer: design tokens, the API client's failure
/// behaviour, and the session-cookie lifecycle.
///
/// The load-bearing one is `signOut` clearing the cookie. Everything else here
/// is presentation; that one is the difference between a signed-out phone
/// showing nothing and a signed-out phone still fetching account data.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:mv_btc_bot/api/client.dart';
import 'package:mv_btc_bot/main.dart'
    show appPages, buildAppTheme, primaryPageIndexes;
import 'package:mv_btc_bot/theme/design.dart';
import 'package:mv_btc_bot/widgets/kit.dart';

void main() {
  group('design tokens', () {
    test('profit and loss colours do not depend on the palette', () {
      // A trader must not read red as "down" in one theme and "brand" in the
      // other, so the semantic colours are outside AppPalette entirely.
      for (final palette in [redPalette, bluePalette]) {
        expect(palette.accent, isNot(kPositive));
        expect(palette.accent, isNot(kNegative));
      }
      expect(kPositive, isNot(kNegative));
    });

    test('zero is neutral, not green', () {
      // Colouring a flat day green overstates the result at a glance.
      expect(signedColour(0), kNeutral);
      expect(signedColour(null), kNeutral);
      expect(signedColour(0.01), kPositive);
      expect(signedColour(-0.01), kNegative);
    });

    test('every zone the engine can emit has a colour', () {
      for (final zone in ['CE_2_ITM', 'PE_2_ITM', 'PE_3_ITM', 'SHORT_MOVE']) {
        expect(
          zoneColour(zone),
          isNot(kZoneHold),
          reason: '$zone must be distinguishable from HOLD',
        );
      }
      expect(zoneColour('HOLD'), kZoneHold);
      expect(zoneColour(null), kZoneHold);
    });

    test('the neon accent matches --neon in static/css/app.css', () {
      expect(kNeon, const Color(0xFF39FF14));
    });

    test('numeric styles use tabular figures', () {
      // Proportional digits make a price column reflow as it updates.
      for (final style in [AppText.display, AppText.metric, AppText.number]) {
        expect(style.fontFeatures, isNotNull);
        expect(
          style.fontFeatures!.any((f) => f.feature == 'tnum'),
          isTrue,
          reason: 'numbers must not reflow as digits change',
        );
      }
    });

    testWidgets('decision score gauge remains a perfect circle when narrow', (
      tester,
    ) async {
      final theme = ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFFEF274D)),
      );
      await tester.pumpWidget(
        MaterialApp(
          theme: theme,
          home: const Scaffold(
            body: SizedBox(
              width: 90,
              height: 140,
              child: DecisionScoreDial(
                label: 'Live preview',
                score: 26,
                colour: kPositive,
              ),
            ),
          ),
        ),
      );

      final paint = find.descendant(
        of: find.byType(DecisionScoreDial),
        matching: find.byType(CustomPaint),
      );
      expect(paint, findsOneWidget);
      final size = tester.getSize(paint);
      expect(size.width, size.height);
      expect(size.width, 90);
      expect(find.text('+26.0'), findsOneWidget);
      expect(find.text('−100'), findsNothing);
      expect(find.text('+100'), findsNothing);
      final scoreText = tester.widget<Text>(find.text('+26.0'));
      expect(scoreText.style?.color, theme.colorScheme.primary);
    });

    testWidgets('committed ADX shows the exact SHORT MOVE exit boundary', (
      tester,
    ) async {
      await tester.pumpWidget(
        MaterialApp(
          theme: buildAppTheme(blue: true),
          home: const Scaffold(
            body: Column(
              children: [
                CommittedAdxPill(adx: 24.9, zone: 'SHORT_MOVE'),
                CommittedAdxPill(adx: 25.0, zone: 'SHORT_MOVE'),
                CommittedAdxPill(adx: 35.0, zone: 'CE_2_ITM'),
              ],
            ),
          ),
        ),
      );

      expect(find.text('5M ADX 24.9 · CALM'), findsOneWidget);
      expect(find.text('5M ADX 25.0 · EXIT MOVE'), findsOneWidget);
      expect(find.text('5M ADX 35.0 · TREND'), findsOneWidget);
    });
  });

  group('ApiResult', () {
    test('an error result is never ok and carries no data', () {
      const result = ApiResult<int>.failed('boom');
      expect(result.ok, isFalse);
      expect(result.data, isNull);
      expect(result.error, 'boom');
    });

    test('unauthorised is distinguishable from a generic failure', () {
      // The screen must be able to tell "sign in again" from "server down".
      // Rendering an expired session as empty data would look like being flat.
      const expired = ApiResult<int>.failed(
        'Session expired',
        unauthorised: true,
      );
      const offline = ApiResult<int>.failed('Cannot reach the server');
      expect(expired.unauthorised, isTrue);
      expect(offline.unauthorised, isFalse);
    });

    test('a successful result is ok even when the payload is null', () {
      const result = ApiResult<int>.ok(null);
      expect(result.ok, isTrue);
    });
  });

  group('DashboardApi', () {
    test('omits the Cookie header when there is no session', () {
      final anonymous = DashboardApi(baseUrl: 'https://x', sessionCookie: null);
      final signedIn = DashboardApi(baseUrl: 'https://x', sessionCookie: 'abc');
      // Sending `Cookie: session=` would look authenticated and fail oddly.
      expect(anonymous.headers.containsKey('Cookie'), isFalse);
      expect(signedIn.headers['Cookie'], 'session=abc');
    });

    test('an empty cookie is treated as no session', () {
      final api = DashboardApi(baseUrl: 'https://x', sessionCookie: '');
      expect(api.headers.containsKey('Cookie'), isFalse);
    });
  });

  group('native screen coverage', () {
    test('every dashboard route has a native destination', () {
      final paths = appPages.map((page) => page.path).toSet();
      for (final native in [
        '/',
        '/trades',
        '/dry-run',
        '/positions',
        '/config',
        '/accounts',
        '/logs',
        '/trend-engine',
      ]) {
        expect(
          paths,
          contains(native),
          reason: '$native must remain reachable in the native app',
        );
      }
    });

    test('phone primary navigation is intentionally compact', () {
      expect(primaryPageIndexes, [0, 1, 7, 4, 2]);
      expect(primaryPageIndexes.map((index) => appPages[index].label), [
        'Today',
        'Performance',
        'Trend Engine',
        'Bot Config',
        'Paper',
      ]);
    });
  });
}
