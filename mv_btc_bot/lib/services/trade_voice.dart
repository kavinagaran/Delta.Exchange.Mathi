/// Foreground voice alerts; no trading or protection side effects.
library;

import 'dart:async';
import 'package:flutter_tts/flutter_tts.dart';

class TradeVoiceAnnouncements {
  TradeVoiceAnnouncements({
    Future<void> Function(String)? speak,
    Future<void> Function()? stop,
    DateTime Function()? now,
  }) : _clock = now ?? DateTime.now {
    final tts = speak == null ? FlutterTts() : null;
    _speaker =
        speak ??
        (message) async {
          await tts!.setLanguage('en-US');
          await tts.setSpeechRate(.48);
          await tts.awaitSpeakCompletion(true);
          await tts.speak(message);
        };
    _stop =
        stop ??
        () async {
          await tts?.stop();
        };
    _instances.add(this);
  }

  static final _instances = <TradeVoiceAnnouncements>{};
  static void setEnabledForAll(bool value) {
    for (final instance in _instances) {
      instance.setEnabled(value);
    }
  }

  static const _interval = Duration(minutes: 15);
  final DateTime Function() _clock;
  late final Future<void> Function(String) _speaker;
  late final Future<void> Function() _stop;
  Future<void> _queue = Future<void>.value();
  bool _enabled = false, _initialised = false, _disposed = false;
  int _revision = 0;
  Object? _mode;
  Map<String, Map<String, dynamic>> _active = {};
  final _pending = <String>{};
  DateTime? _lastPnlAt;

  void setEnabled(bool value) {
    if (_disposed) return;
    if (_enabled != value) {
      _enabled = value;
      reset();
    }
    if (!value) unawaited(_stop().catchError((Object _) {}));
  }

  void configure(bool enabled, Object? mode) {
    setEnabled(enabled);
    if (_mode != mode) {
      _mode = mode;
      reset();
    }
  }

  void reset() {
    _revision++;
    _initialised = false;
    _active.clear();
    _pending.clear();
    unawaited(_stop().catchError((Object _) {}));
  }

  Future<void> observe(List<dynamic> rows) {
    if (_disposed) return Future<void>.value();
    final trades = rows.whereType<Map<String, dynamic>>().toList();
    final open = {
      for (final t in trades)
        if (_isOpen(t)) _id(t): t,
    };
    final now = _clock();
    if (!_enabled || !_initialised) {
      _initialised = true;
      _active = open;
      _pending.clear();
      _lastPnlAt = now;
      return Future<void>.value();
    }
    for (final key in _active.keys) {
      if (!open.containsKey(key)) _pending.add(key);
    }
    final messages = <String>[];
    for (final t in trades) {
      final key = _id(t), booked = _pnl(t);
      // A missing row is not proof of an exit. Wait for reconciled history.
      if (!_isOpen(t) && _pending.contains(key) && booked != null) {
        messages.add(
          'Trade closed. ${t['symbol'] ?? 'Option trade'}. Booked ${_dollars(booked)}.',
        );
        _pending.remove(key);
      }
    }
    for (final entry in open.entries) {
      final t = entry.value;
      if (!_active.containsKey(entry.key) && !_pending.contains(entry.key)) {
        messages.add(
          'Trade taken. ${t['symbol'] ?? 'Option trade'}, ${t['side'] ?? ''}, ${t['lots'] ?? 0} lots.',
        );
        _lastPnlAt = now;
      }
      _pending.remove(entry.key);
    }
    _active = open;
    if (_lastPnlAt != null && now.difference(_lastPnlAt!) >= _interval) {
      for (final t in open.values) {
        final amount = _pnl(t);
        if (amount != null) {
          messages.add(
            'Current trade ${t['symbol'] ?? ''}. ${_dollars(amount)}.',
          );
        }
      }
      _lastPnlAt = now;
    }
    final version = _revision;
    for (final message in messages) {
      _queue = _queue
          .then((_) async {
            if (!_disposed && _enabled && version == _revision) {
              await _speaker(message);
            }
          })
          .catchError((Object _) {
            /* speech failure must not affect trading UI */
          });
    }
    return _queue;
  }

  Future<void> dispose() async {
    _disposed = true;
    _revision++;
    _instances.remove(this);
    await _stop().catchError((Object _) {});
  }

  static bool _isOpen(Map<String, dynamic> t) =>
      t['_live'] == true || '${t['status']}'.toUpperCase() == 'OPEN';

  static String _id(Map<String, dynamic> t) =>
      '${t['position_cycle_id'] ?? t['simulation_id'] ?? t['trade_id'] ?? [t['symbol'], t['entry_date'] ?? t['date'], t['entry_time_utc'] ?? t['entry_time']].join('|')}';

  static double? _pnl(Map<String, dynamic> t) {
    final keys = _isOpen(t)
        ? const ['live_pnl', 'pnl_usd', 'net_pnl', 'gross_pnl_usd']
        : const ['pnl_usd', 'net_pnl', 'gross_pnl_usd'];
    for (final key in keys) {
      final value = double.tryParse('${t[key]}');
      if (value != null && value.isFinite) return value;
    }
    return null;
  }

  static String _dollars(double n) =>
      '${n >= 0 ? 'profit' : 'loss'} of ${n.abs().toStringAsFixed(2)} dollars';
}
