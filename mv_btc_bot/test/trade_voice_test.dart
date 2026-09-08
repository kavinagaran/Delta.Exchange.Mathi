import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:mv_btc_bot/services/trade_voice.dart';

Map<String, dynamic> trade([String id = 'a']) => {
  'position_cycle_id': id,
  'symbol': 'C-BTC',
  'status': 'OPEN',
  'lots': 250,
  'live_pnl': 12.5,
};

void main() {
  test('entry, timed P&L, mute and no replay', () async {
    var now = DateTime(2026);
    final spoken = <String>[];
    var stopped = 0;
    final voice = TradeVoiceAnnouncements(
      speak: (text) async {
        spoken.add(text);
      },
      stop: () async {
        stopped++;
      },
      now: () => now,
    );
    voice.setEnabled(true);
    await voice.observe([]);
    await voice.observe([trade()]);
    expect(spoken.single, contains('250 lots'));
    now = now.add(const Duration(minutes: 15));
    await voice.observe([trade()]);
    expect(spoken.last, contains('profit of 12.50 dollars'));
    TradeVoiceAnnouncements.setEnabledForAll(false);
    expect(stopped, greaterThan(0));
    await voice.observe([]);
    voice.setEnabled(true);
    await voice.observe([trade('b')]);
    expect(spoken.length, 2);
    await voice.dispose();
  });
  test('missing row and unrealized value are not booked P&L', () async {
    final spoken = <String>[];
    final voice = TradeVoiceAnnouncements(
      speak: (s) async {
        spoken.add(s);
      },
    );
    voice.setEnabled(true);
    await voice.observe([trade()]);
    await voice.observe([]);
    final closed = {...trade(), 'status': 'CLOSED', 'pnl_usd': null};
    await voice.observe([closed]);
    expect(spoken, isEmpty);
    await voice.observe([
      {...closed, 'pnl_usd': -5.25},
      trade('b'),
    ]);
    expect(spoken.first, contains('Booked loss of 5.25 dollars'));
    expect(spoken.last, contains('Trade taken'));
    await voice.dispose();
  });
  test('mute stops queued announcements during speech', () async {
    final spoken = <String>[];
    final gate = Completer<void>();
    final voice = TradeVoiceAnnouncements(
      speak: (s) async {
        spoken.add(s);
        await gate.future;
      },
    );
    voice.setEnabled(true);
    await voice.observe([]);
    final pending = voice.observe([trade(), trade('b')]);
    await Future<void>.delayed(Duration.zero);
    voice.setEnabled(false);
    gate.complete();
    await pending;
    expect(spoken.length, 1);
    await voice.dispose();
  });
}
