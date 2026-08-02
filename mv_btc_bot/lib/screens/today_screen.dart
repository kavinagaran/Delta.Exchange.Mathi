/// Today — the at-a-glance screen.
///
/// Native rather than embedded because this is the one view that gets opened
/// on a phone in a hurry: it has to answer "is the bot alive, am I in a
/// position, and what is it doing" inside one screen, with no horizontal
/// scrolling and no waiting for a WebView to lay out a desktop grid.
///
/// Every field is optional on purpose. The dashboard has several trading modes
/// and a degraded engine, so a screen that assumes a key exists will crash on
/// exactly the day something is wrong.
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../theme/design.dart';
import '../widgets/kit.dart';

class TodayScreen extends StatefulWidget {
  const TodayScreen({
    super.key,
    required this.api,
    required this.onUnauthorised,
  });

  final DashboardApi api;
  final VoidCallback onUnauthorised;

  @override
  State<TodayScreen> createState() => _TodayScreenState();
}

class _TodayScreenState extends State<TodayScreen> {
  Map<String, dynamic>? _status;
  List<dynamic> _todayTrades = const [];
  String? _error;
  bool _loading = true;
  bool _closing = false;
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    _refresh();
    // 20s: fast enough that a fill shows up while you are looking at the
    // screen, slow enough not to hammer a dashboard that also runs the
    // trading loop in-process.
    _poll = Timer.periodic(
      const Duration(seconds: 20),
      (_) => _refresh(quiet: true),
    );
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _refresh({bool quiet = false}) async {
    if (!quiet && mounted) setState(() => _loading = true);

    final results = await Future.wait([
      widget.api.status(),
      widget.api.todayTrades(),
    ]);
    if (!mounted) return;

    // One expired session anywhere means re-authenticate, not a half-blank
    // screen showing stale numbers as if they were current.
    if (results.any((r) => r.unauthorised)) {
      widget.onUnauthorised();
      return;
    }

    setState(() {
      _loading = false;
      _status = results[0].data as Map<String, dynamic>?;
      _todayTrades = (results[1].data as List<dynamic>?) ?? const [];
      // Only the primary call's failure blanks the screen; the engine being
      // unreachable is itself information and gets its own card.
      _error = results[0].ok ? null : results[0].error;
    });
  }

  Map<String, dynamic>? get _currentTrade {
    for (final row in _todayTrades.whereType<Map<String, dynamic>>()) {
      if (row['_live'] == true || '${row['status']}'.toUpperCase() == 'OPEN') {
        return row;
      }
    }
    return null;
  }

  Map<String, dynamic>? get _latestTrade {
    final closed = _todayTrades
        .whereType<Map<String, dynamic>>()
        .where(
          (row) =>
              row['_live'] != true &&
              '${row['status'] ?? 'CLOSED'}'.toUpperCase() != 'OPEN',
        )
        .toList();
    if (closed.isEmpty) return null;
    closed.sort(
      (left, right) => _tradeMoment(
        left,
        exit: true,
      ).compareTo(_tradeMoment(right, exit: true)),
    );
    return closed.last;
  }

  Future<void> _close(Map<String, dynamic> trade) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Close live position?'),
        content: Text('${trade['symbol'] ?? 'Current position'} · market exit'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Close'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() => _closing = true);
    final slot = '${trade['control_slot'] ?? trade['slot'] ?? 'trend'}';
    final result = await widget.api.squareOff(slot: slot, targetMode: 'live');
    if (!mounted) return;
    setState(() => _closing = false);
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          result.ok ? 'Close submitted' : result.error ?? 'Close failed',
        ),
        backgroundColor: result.ok ? kPositive : kNegative,
      ),
    );
    if (result.ok) await _refresh(quiet: true);
  }

  @override
  Widget build(BuildContext context) {
    if (_loading && _status == null) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    }
    if (_error != null && _status == null) {
      return StatePlaceholder(
        icon: Icons.cloud_off_rounded,
        message: 'Cannot load today',
        detail: _error,
        onRetry: _refresh,
        tone: kNegative,
      );
    }

    final current = _currentTrade;
    final latest = _latestTrade;
    return RefreshIndicator(
      onRefresh: _refresh,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.md, Gap.lg, Gap.xxl),
        children: [
          if (current == null)
            const AppCard(
              kicker: 'Current trade',
              title: 'Flat',
              child: Text('No live position.', style: AppText.body),
            )
          else
            _CurrentTradeCard(
              trade: current,
              busy: _closing,
              onClose: () => _close(current),
            ),
          const SizedBox(height: Gap.md),
          _LatestTradeCard(trade: latest),
        ],
      ),
    );
  }
}

class _CurrentTradeCard extends StatelessWidget {
  const _CurrentTradeCard({
    required this.trade,
    required this.busy,
    required this.onClose,
  });

  final Map<String, dynamic> trade;
  final bool busy;
  final VoidCallback onClose;

  @override
  Widget build(BuildContext context) {
    final pnl = (trade['live_pnl'] as num?)?.toDouble();
    return AppCard(
      kicker: 'Current trade',
      title: '${trade['symbol'] ?? '—'}',
      accent: signedColour(pnl),
      trailing: const StatusPill('LIVE', colour: kPositive),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          MetricTile(
            label: 'Live P&L',
            value: pnl == null ? '—' : '\$${_signed(pnl, 2)}',
            colour: signedColour(pnl),
            big: true,
          ),
          const SizedBox(height: Gap.md),
          MetricWrap(
            children: [
              MetricTile(label: 'Lots', value: '${trade['lots'] ?? '—'}'),
              MetricTile(
                label: 'Side',
                value: '${trade['side'] ?? '—'}'.toUpperCase(),
              ),
              MetricTile(
                label: 'Entry',
                value: _tradePrice(trade['entry_mark']),
              ),
              MetricTile(
                label: 'Mark',
                value: _tradePrice(trade['current_mark']),
              ),
            ],
          ),
          const SizedBox(height: Gap.lg),
          Center(
            child: CompactAction(
              label: busy ? 'Exiting…' : 'Exit',
              icon: Icons.exit_to_app_rounded,
              tone: kNegative,
              filled: true,
              onPressed: busy ? null : onClose,
            ),
          ),
        ],
      ),
    );
  }
}

/// Engine state: score, zone, and whether an entry is currently permitted.
// ignore: unused_element
class _EngineCard extends StatelessWidget {
  const _EngineCard({required this.engine, required this.controller});

  final Map<String, dynamic>? engine;
  final Map<String, dynamic>? controller;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;

    if (engine == null) {
      return const AppCard(
        kicker: 'Trend engine',
        title: 'Engine unreachable',
        accent: kNegative,
        child: Text(
          'No snapshot. Entries fail closed while the engine is unreachable, '
          'so nothing will be opened until it returns.',
          style: AppText.body,
        ),
      );
    }

    final score = (engine!['trend_score'] as num?)?.toDouble();
    final zone = engine!['zone'] as String?;
    final quality = engine!['data_quality'] as String?;
    final allowed = engine!['zone_action_allowed'] == true;
    final reason = engine!['zone_reason'] as String?;
    final healthy = quality == 'OK';

    return AppCard(
      kicker: 'Trend engine',
      title: zone == null ? 'No zone' : _zoneLabel(zone),
      accent: zoneColour(zone),
      trailing: StatusPill(
        healthy ? (allowed ? 'Armed' : 'Holding') : (quality ?? 'Degraded'),
        colour: healthy ? (allowed ? kPositive : kWarning) : kNegative,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Expanded(
                child: MetricTile(
                  label: 'Decision score',
                  value: score == null ? '—' : _signed(score, 1),
                  colour: signedColour(score),
                  big: true,
                ),
              ),
              if (controller?['status'] != null)
                Padding(
                  padding: const EdgeInsets.only(bottom: 6),
                  child: StatusPill(
                    '${controller!['status']}'.replaceAll('_', ' '),
                    colour: scheme.onSurfaceVariant,
                    dot: false,
                  ),
                ),
            ],
          ),
          const SizedBox(height: Gap.md),
          if (score != null) ScoreMeter(score: score),
          if (reason != null && reason.isNotEmpty) ...[
            const SizedBox(height: Gap.sm),
            Text(
              reason,
              style: AppText.caption.copyWith(color: scheme.onSurfaceVariant),
            ),
          ],
        ],
      ),
    );
  }

  static String _zoneLabel(String zone) => switch (zone) {
    'CE_2_ITM' => 'Bullish · buy 2-step ITM call',
    'PE_2_ITM' || 'PE_3_ITM' => 'Bearish · buy 2-step ITM put',
    'SHORT_MOVE' => 'Sideways · sell ATM MOVE',
    'HOLD' => 'Hold · no new action',
    _ => zone,
  };
}

/// Open positions across every slot, flattened for a phone.
// ignore: unused_element
class _PositionsCard extends StatelessWidget {
  const _PositionsCard({required this.status});

  final Map<String, dynamic>? status;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    // /api/status nests `trend` and `morning` but keeps the evening slot at
    // the root of the payload, so the three are collected explicitly rather
    // than by a loop that would have to special-case one of them anyway.
    final candidates = <String, Object?>{
      'trend': status?['trend'],
      'morning': status?['morning'],
      'evening': status,
    };
    final slots = <String, Map<String, dynamic>>{};
    candidates.forEach((name, slot) {
      if (slot is Map<String, dynamic> && slot['status'] == 'OPEN') {
        slots[name] = slot;
      }
    });

    if (slots.isEmpty) {
      return const AppCard(
        kicker: 'Exposure',
        title: 'No open position',
        child: Text(
          'Flat. The controller opens on the next qualifying closed candle.',
          style: AppText.body,
        ),
      );
    }

    return AppCard(
      kicker: 'Exposure',
      title: slots.length == 1
          ? 'Open position'
          : '${slots.length} open positions',
      accent: kPositive,
      child: Column(
        children: [
          for (final entry in slots.entries) ...[
            _PositionRow(slot: entry.key, position: entry.value),
            if (entry.key != slots.keys.last)
              Divider(height: Gap.xl, color: scheme.outline),
          ],
        ],
      ),
    );
  }
}

class _PositionRow extends StatelessWidget {
  const _PositionRow({required this.slot, required this.position});

  final String slot;
  final Map<String, dynamic> position;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final pnl = (position['pnl_usd'] as num?)?.toDouble();
    final symbol = position['symbol'] as String? ?? '—';
    final lots = position['lots'];
    final entry = (position['entry_mark'] as num?)?.toDouble();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    slot.toUpperCase(),
                    style: AppText.kicker.copyWith(
                      color: scheme.onSurfaceVariant,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(symbol, style: AppText.title),
                ],
              ),
            ),
            Text(
              pnl == null ? '—' : _signed(pnl, 2),
              style: AppText.metric.copyWith(color: signedColour(pnl)),
            ),
          ],
        ),
        const SizedBox(height: Gap.sm),
        if (lots != null) StatRow('Lots', '$lots'),
        if (entry != null) StatRow('Entry', entry.toStringAsFixed(1)),
      ],
    );
  }
}

/// The most recent completed trade today, presented with the same strong
/// hierarchy as the open-position card instead of being buried in a list.
class _LatestTradeCard extends StatelessWidget {
  const _LatestTradeCard({required this.trade});

  final Map<String, dynamic>? trade;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final row = trade;
    if (row == null) {
      return AppCard(
        title: 'Latest Trade',
        child: Text(
          'No completed trade today.',
          style: AppText.body.copyWith(color: scheme.onSurfaceVariant),
        ),
      );
    }

    final pnl = _number(row['pnl_usd']);
    final result = pnl == null
        ? 'CLOSED'
        : pnl > 0
        ? 'WIN'
        : pnl < 0
        ? 'LOSS'
        : 'FLAT';
    final resultColour = pnl == null ? kNeutral : signedColour(pnl);

    return AppCard(
      title: 'Latest Trade',
      accent: resultColour,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  'LAST COMPLETED POSITION',
                  style: AppText.kicker.copyWith(
                    color: scheme.onSurfaceVariant,
                  ),
                ),
              ),
              StatusPill(result, colour: resultColour, dot: false),
            ],
          ),
          const SizedBox(height: Gap.lg),
          Center(
            child: Column(
              children: [
                Text(
                  pnl == null ? '—' : _money(pnl),
                  style: AppText.display.copyWith(color: resultColour),
                ),
                const SizedBox(height: 2),
                Text(
                  'REALIZED P&L',
                  style: AppText.kicker.copyWith(
                    color: scheme.onSurfaceVariant,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: Gap.lg),
          StatRow('Trade date', _tradeDate(row)),
          StatRow('Entry time', _tradeTime(row)),
          StatRow('Exit time', _tradeTime(row, exit: true)),
          StatRow('Trade type', _tradeType(row), valueColour: resultColour),
          StatRow('Contract', '${row['symbol'] ?? '—'}'),
          StatRow(
            'Side',
            '${row['side'] ?? '—'}'.toUpperCase(),
            valueColour: _sideColour(row['side']),
          ),
          StatRow('Lots', _lots(row['lots'])),
          StatRow('Entry', _tradePrice(row['entry_mark'])),
          StatRow('Exit price', _tradePrice(row['exit_mark'])),
          if ('${row['exit_trigger'] ?? ''}'.trim().isNotEmpty)
            StatRow(
              'Exit reason',
              '${row['exit_trigger']}'.replaceAll('_', ' ').toUpperCase(),
              mono: false,
            ),
        ],
      ),
    );
  }
}

/// Always show the sign. A P&L column where losses are signed and gains are
/// bare reads as though gains are the default state.
String _signed(double value, int digits) =>
    '${value > 0 ? '+' : ''}${value.toStringAsFixed(digits)}';

String _money(double value) =>
    '${value > 0
        ? '+'
        : value < 0
        ? '-'
        : ''}'
    '\$${value.abs().toStringAsFixed(2)}';

String _tradePrice(Object? value) {
  final number = _number(value);
  return number == null ? '—' : '\$${number.toStringAsFixed(2)}';
}

double? _number(Object? value) =>
    value is num ? value.toDouble() : double.tryParse('$value');

String _tradeType(Map<String, dynamic> trade) {
  final zone = '${trade['trend_score_zone'] ?? trade['engine_zone'] ?? ''}'
      .toUpperCase();
  final symbol = '${trade['symbol'] ?? ''}'.toUpperCase();
  if (zone.startsWith('CE') || symbol.startsWith('C-')) return 'CE';
  if (zone.startsWith('PE') || symbol.startsWith('P-')) return 'PE';
  if (zone == 'SHORT_MOVE' || symbol.startsWith('MV-')) return 'MV';
  return 'TRADE';
}

Color _sideColour(Object? side) =>
    '$side'.toLowerCase() == 'short' ? kNegative : kPositive;

String _lots(Object? value) {
  final number = _number(value);
  if (number == null) return '—';
  final raw = number == number.roundToDouble()
      ? number.toInt().toString()
      : number.toString();
  return raw.replaceAllMapped(RegExp(r'\B(?=(\d{3})+(?!\d))'), (_) => ',');
}

DateTime _tradeMoment(Map<String, dynamic> trade, {required bool exit}) {
  final iso =
      '${exit ? trade['exit_at_utc'] ?? trade['closed_at_utc'] ?? '' : trade['entry_at_utc'] ?? trade['opened_at_utc'] ?? ''}'
          .trim();
  final parsedIso = DateTime.tryParse(iso);
  if (parsedIso != null) return parsedIso.toUtc();

  final date =
      '${exit ? trade['exit_date'] ?? trade['entry_date'] ?? trade['date'] ?? '' : trade['entry_date'] ?? trade['date'] ?? ''}'
          .trim();
  final time =
      '${exit ? trade['exit_time_utc'] ?? trade['exit_time'] ?? '' : trade['entry_time_utc'] ?? trade['entry_time'] ?? ''}'
          .trim();
  return DateTime.tryParse('${date}T${time}Z')?.toUtc() ??
      DateTime.fromMillisecondsSinceEpoch(0, isUtc: true);
}

String _tradeDate(Map<String, dynamic> trade) {
  final moment = _tradeMoment(trade, exit: false);
  if (moment.millisecondsSinceEpoch == 0) return '—';
  final ist = moment.add(const Duration(hours: 5, minutes: 30));
  return '${ist.year.toString().padLeft(4, '0')}-'
      '${ist.month.toString().padLeft(2, '0')}-'
      '${ist.day.toString().padLeft(2, '0')}';
}

String _tradeTime(Map<String, dynamic> trade, {bool exit = false}) {
  final moment = _tradeMoment(trade, exit: exit);
  if (moment.millisecondsSinceEpoch == 0) return '—';
  final ist = moment.add(const Duration(hours: 5, minutes: 30));
  final hour = ist.hour % 12 == 0 ? 12 : ist.hour % 12;
  final minute = ist.minute.toString().padLeft(2, '0');
  return '$hour:$minute ${ist.hour < 12 ? 'AM' : 'PM'} IST';
}
