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
  Map<String, dynamic>? _engine;
  Map<String, dynamic>? _engineLive;
  Map<String, dynamic>? _protection;
  List<dynamic> _todayTrades = const [];
  String? _error;
  bool _loading = true;
  bool _closing = false;
  Timer? _poll;
  Timer? _previewPoll;
  bool _previewLoading = false;
  Timer? _streamReconnect;
  StreamSubscription<ApiResult<Map<String, dynamic>>>? _protectionEvents;

  @override
  void initState() {
    super.initState();
    _refresh();
    _connectProtectionStream();
    // 20s: fast enough that a fill shows up while you are looking at the
    // screen, slow enough not to hammer a dashboard that also runs the
    // trading loop in-process.
    _poll = Timer.periodic(
      const Duration(seconds: 20),
      (_) => _refresh(quiet: true),
    );
    // The forming-candle preview is display-only and cheap to refresh. Keep it
    // separate from the heavier account/trade refresh so the dial feels live.
    _previewPoll = Timer.periodic(
      const Duration(seconds: 2),
      (_) => _refreshPreview(),
    );
  }

  @override
  void dispose() {
    _poll?.cancel();
    _previewPoll?.cancel();
    _streamReconnect?.cancel();
    _protectionEvents?.cancel();
    super.dispose();
  }

  Future<void> _refreshPreview() async {
    if (_previewLoading) return;
    _previewLoading = true;
    final result = await widget.api.engineLive();
    _previewLoading = false;
    if (!mounted) return;
    if (result.unauthorised) {
      widget.onUnauthorised();
      return;
    }
    if (result.ok && result.data != null) {
      setState(() => _engineLive = result.data);
    }
  }

  void _connectProtectionStream() {
    _streamReconnect?.cancel();
    _protectionEvents?.cancel();
    _protectionEvents = widget.api.protectionStream().listen(
      (result) {
        if (!mounted) return;
        if (result.unauthorised) {
          widget.onUnauthorised();
          return;
        }
        if (!result.ok || result.data == null) return;
        _applyProtectionSnapshot(result.data!);
      },
      onError: (_) => _scheduleStreamReconnect(),
      onDone: _scheduleStreamReconnect,
      cancelOnError: false,
    );
  }

  void _scheduleStreamReconnect() {
    if (!mounted) return;
    _streamReconnect?.cancel();
    _streamReconnect = Timer(
      const Duration(seconds: 2),
      _connectProtectionStream,
    );
  }

  void _applyProtectionSnapshot(Map<String, dynamic> payload) {
    final nextTrades = _todayTrades
        .map(
          (row) =>
              row is Map<String, dynamic> ? <String, dynamic>{...row} : row,
        )
        .toList();
    for (final row in nextTrades.whereType<Map<String, dynamic>>()) {
      if (!_isOpen(row) || row['dry_run'] == true) continue;
      final slot = '${row['control_slot'] ?? row['slot'] ?? 'trend'}';
      final record = payload[slot];
      if (record is! Map<String, dynamic>) continue;
      if (record['streaming'] == true) {
        final mark = _number(record['live_mark']);
        final pnl = _number(record['live_pnl']);
        if (mark != null) row['current_mark'] = mark;
        if (pnl != null) row['live_pnl'] = pnl;
      }
    }
    setState(() {
      _protection = payload;
      _todayTrades = nextTrades;
    });
  }

  Future<void> _refresh({bool quiet = false}) async {
    if (!quiet && mounted) setState(() => _loading = true);

    final results = await Future.wait([
      widget.api.status(),
      widget.api.todayTrades(),
      widget.api.engineSnapshot(),
      widget.api.engineLive(),
      widget.api.protectionStatus(),
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
      _engine = results[2].data as Map<String, dynamic>?;
      _engineLive = results[3].data as Map<String, dynamic>?;
      _protection = results[4].data as Map<String, dynamic>?;
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
    final trades = _todayTrades.whereType<Map<String, dynamic>>().toList()
      ..sort(
        (left, right) => _tradeMoment(
          right,
          exit: false,
        ).compareTo(_tradeMoment(left, exit: false)),
      );
    final closed = trades.where((row) => !_isOpen(row)).length;
    final dayPnl = trades.fold<double>(
      0,
      (total, row) =>
          total +
          (_number(row['pnl_usd'] ?? row['net_pnl'] ?? row['live_pnl']) ?? 0),
    );
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
              protection: _protectionFor(current),
              busy: _closing,
              onClose: () => _close(current),
            ),
          const SizedBox(height: Gap.md),
          _EngineCard(engine: _engine, live: _engineLive),
          const SizedBox(height: Gap.md),
          MetricWrap(
            children: [
              MetricTile(label: 'Trades', value: '${trades.length}'),
              MetricTile(label: 'Open', value: current == null ? '0' : '1'),
              MetricTile(label: 'Closed', value: '$closed'),
              MetricTile(
                label: 'Day P&L',
                value: _money(dayPnl),
                colour: signedColour(dayPnl),
              ),
            ],
          ),
          const SizedBox(height: Gap.md),
          _TodayTradesCard(trades: trades),
        ],
      ),
    );
  }

  Map<String, dynamic>? _protectionFor(Map<String, dynamic> trade) {
    final embedded = trade['dry_protection'];
    if (embedded is Map<String, dynamic>) return embedded;
    final slot = '${trade['control_slot'] ?? trade['slot'] ?? 'trend'}';
    final value = _protection?[slot];
    return value is Map<String, dynamic> ? value : null;
  }
}

bool _isOpen(Map<String, dynamic> row) =>
    row['_live'] == true || '${row['status']}'.toUpperCase() == 'OPEN';

class _CurrentTradeCard extends StatelessWidget {
  const _CurrentTradeCard({
    required this.trade,
    required this.protection,
    required this.busy,
    required this.onClose,
  });

  final Map<String, dynamic> trade;
  final Map<String, dynamic>? protection;
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
          if (protection != null) ...[
            const SizedBox(height: Gap.lg),
            _ProtectionPanel(protection: protection!),
          ],
        ],
      ),
    );
  }
}

class _ProtectionPanel extends StatelessWidget {
  const _ProtectionPanel({required this.protection});

  final Map<String, dynamic> protection;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final running = protection['running'] == true;
    final armed =
        protection['stream_tsl_armed'] == true ||
        protection['tsl_armed'] == true;
    String value(String key, [String? fallback]) {
      final raw =
          protection[key] ?? (fallback == null ? null : protection[fallback]);
      final number = _number(raw);
      return number == null ? '—' : '\$${number.toStringAsFixed(2)}';
    }

    return Container(
      padding: const EdgeInsets.all(Gap.md),
      decoration: BoxDecoration(
        color: scheme.surfaceContainerHighest.withValues(alpha: .52),
        borderRadius: BorderRadius.circular(Radii.md),
        border: Border.all(color: scheme.outline),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Expanded(
                child: Text('TP / SL / TSL Monitor', style: AppText.title),
              ),
              StatusPill(
                running ? (armed ? 'TSL ARMED' : 'RUNNING') : 'VERIFYING',
                colour: running ? (armed ? kWarning : kPositive) : kNeutral,
              ),
            ],
          ),
          const SizedBox(height: Gap.md),
          MetricWrap(
            children: [
              MetricTile(
                label: 'Take profit',
                value: value('target_pnl', 'tp_target_pnl'),
                colour: kPositive,
              ),
              MetricTile(
                label: 'Stop loss',
                value: value('sl_pnl', 'sl_target_pnl'),
                colour: kNegative,
              ),
              MetricTile(
                label: 'TSL arm',
                value: value('tsl_arm_pnl'),
                colour: kWarning,
              ),
              MetricTile(
                label: 'TSL trail',
                value: value('tsl_trail_pnl'),
                colour: const Color(0xFF70B8FF),
              ),
              MetricTile(
                label: 'Minimum lock',
                value: value('tsl_lock_min_pnl'),
                colour: const Color(0xFFC58CFF),
              ),
              MetricTile(
                label: 'Price feed',
                value: protection['streaming'] == true
                    ? 'LIVE · ~${protection['stream_expected_interval_secs'] ?? 2}s'
                    : 'WATCHDOG · ${protection['poll_secs'] ?? '—'}s',
                colour: scheme.secondary,
              ),
            ],
          ),
        ],
      ),
    );
  }
}

/// Engine state: score, zone, and whether an entry is currently permitted.
class _EngineCard extends StatelessWidget {
  const _EngineCard({required this.engine, required this.live});

  final Map<String, dynamic>? engine;
  final Map<String, dynamic>? live;

  @override
  Widget build(BuildContext context) {
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

    final score = _number(engine!['trend_score']);
    final preview = _number(
      live?['live_score'] ??
          live?['preview_score'] ??
          live?['trend_score'] ??
          live?['score'],
    );
    final zone = engine!['zone'] as String?;
    final previewZone = _zoneFromScore(preview);

    return AppCard(
      kicker: 'Trend engine',
      title: zone == null ? 'No zone' : _zoneLabel(zone),
      accent: zoneColour(zone),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: DecisionScoreDial(
                  label: 'Live preview',
                  score: preview,
                  colour: zoneColour(previewZone),
                  caption: _shortZone(previewZone),
                  maxSize: 112,
                ),
              ),
              const SizedBox(width: Gap.md),
              Expanded(
                child: DecisionScoreDial(
                  label: 'Committed',
                  score: score,
                  colour: zoneColour(zone),
                  caption: zone == null ? 'No decision' : _shortZone(zone),
                  maxSize: 112,
                ),
              ),
            ],
          ),
          const SizedBox(height: Gap.sm),
          if (score != null) ScoreMeter(score: score),
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

  static String _shortZone(String zone) => switch (zone) {
    'CE_2_ITM' => 'Buy CE',
    'PE_2_ITM' || 'PE_3_ITM' => 'Buy PE',
    'SHORT_MOVE' => 'Short MOVE',
    _ => 'Hold',
  };

  static String _zoneFromScore(double? score) {
    if (score == null) return 'HOLD';
    if (score > 40) return 'CE_2_ITM';
    if (score < -40) return 'PE_2_ITM';
    if (score >= -30 && score <= 30) return 'SHORT_MOVE';
    return 'HOLD';
  }
}

/// Every trade opened today, presented as compact expandable rows so the full
/// web table remains usable on a 360dp phone without horizontal scrolling.
class _TodayTradesCard extends StatelessWidget {
  const _TodayTradesCard({required this.trades});

  final List<Map<String, dynamic>> trades;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    if (trades.isEmpty) {
      return AppCard(
        kicker: "Today's activity",
        title: 'No trades yet',
        child: Text(
          'New entries will appear here automatically.',
          style: AppText.body.copyWith(color: scheme.onSurfaceVariant),
        ),
      );
    }
    return AppCard(
      kicker: "Today's activity",
      title: '${trades.length} ${trades.length == 1 ? 'trade' : 'trades'}',
      trailing: const Icon(Icons.receipt_long_rounded, size: 19),
      padding: const EdgeInsets.fromLTRB(Gap.md, Gap.lg, Gap.md, Gap.sm),
      child: Column(
        children: [
          for (var index = 0; index < trades.length; index++) ...[
            _TradeHistoryRow(trade: trades[index]),
            if (index != trades.length - 1)
              Divider(height: 1, color: scheme.outline),
          ],
        ],
      ),
    );
  }
}

class _TradeHistoryRow extends StatelessWidget {
  const _TradeHistoryRow({required this.trade});

  final Map<String, dynamic> trade;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final open = _isOpen(trade);
    final pnl = _number(
      trade['pnl_usd'] ?? trade['net_pnl'] ?? trade['live_pnl'],
    );
    final status = open
        ? 'OPEN'
        : (pnl ?? 0) > 0
        ? 'WIN'
        : (pnl ?? 0) < 0
        ? 'LOSS'
        : 'CLOSED';
    return Material(
      color: Colors.transparent,
      child: Theme(
        data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
        child: ExpansionTile(
          tilePadding: const EdgeInsets.symmetric(
            horizontal: Gap.xs,
            vertical: 3,
          ),
          childrenPadding: const EdgeInsets.fromLTRB(Gap.sm, 0, Gap.sm, Gap.md),
          iconColor: scheme.primary,
          collapsedIconColor: scheme.onSurfaceVariant,
          title: Text(
            '${trade['symbol'] ?? 'Unknown contract'}',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: AppText.title.copyWith(fontSize: 13),
          ),
          subtitle: Padding(
            padding: const EdgeInsets.only(top: 5),
            child: Row(
              children: [
                StatusPill(
                  _tradeType(trade),
                  colour: zoneColour(switch (_tradeType(trade)) {
                    'CE' => 'CE_2_ITM',
                    'PE' => 'PE_2_ITM',
                    'MV' => 'SHORT_MOVE',
                    _ => 'HOLD',
                  }),
                  dot: false,
                ),
                const SizedBox(width: Gap.sm),
                Expanded(
                  child: Text(
                    '${_tradeTime(trade)}  ·  ${_lots(trade['lots'])} lots',
                    overflow: TextOverflow.ellipsis,
                    style: AppText.caption.copyWith(
                      color: scheme.onSurfaceVariant,
                    ),
                  ),
                ),
              ],
            ),
          ),
          trailing: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Text(
                pnl == null ? '—' : _money(pnl),
                style: AppText.number.copyWith(color: signedColour(pnl)),
              ),
              Text(
                status,
                style: AppText.kicker.copyWith(
                  color: open ? kWarning : signedColour(pnl),
                ),
              ),
            ],
          ),
          children: [
            StatRow('Opened', '${_tradeDate(trade)} · ${_tradeTime(trade)}'),
            StatRow('Closed', open ? '—' : _tradeTime(trade, exit: true)),
            StatRow(
              'Direction',
              '${trade['side'] ?? '—'}'.toUpperCase(),
              valueColour: _sideColour(trade['side']),
            ),
            StatRow('Entry', _tradePrice(trade['entry_mark'])),
            StatRow(
              'Exit / mark',
              _tradePrice(open ? trade['current_mark'] : trade['exit_mark']),
            ),
            if (_number(trade['gross_pnl'] ?? trade['gross_pnl_usd']) != null)
              StatRow(
                'Gross P&L',
                _money(_number(trade['gross_pnl'] ?? trade['gross_pnl_usd'])!),
                valueColour: signedColour(
                  _number(trade['gross_pnl'] ?? trade['gross_pnl_usd']),
                ),
              ),
            if (_number(trade['fees'] ?? trade['fees_usd']) != null)
              StatRow('Fees', _tradePrice(trade['fees'] ?? trade['fees_usd'])),
            if ('${trade['exit_trigger'] ?? ''}'.trim().isNotEmpty)
              StatRow(
                'Exit reason',
                '${trade['exit_trigger']}'.replaceAll('_', ' ').toUpperCase(),
                mono: false,
              ),
          ],
        ),
      ),
    );
  }
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
