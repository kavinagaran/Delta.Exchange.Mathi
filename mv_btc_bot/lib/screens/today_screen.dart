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
  Map<String, dynamic>? _controller;
  List<dynamic> _todayTrades = const [];
  String? _error;
  bool _loading = true;
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    _refresh();
    // 20s: fast enough that a fill shows up while you are looking at the
    // screen, slow enough not to hammer a dashboard that also runs the
    // trading loop in-process.
    _poll = Timer.periodic(const Duration(seconds: 20), (_) => _refresh(quiet: true));
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
      widget.api.engineSnapshot(),
      widget.api.scoreAutoStatus(),
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
      _engine = results[1].data as Map<String, dynamic>?;
      _controller = results[2].data as Map<String, dynamic>?;
      _todayTrades = (results[3].data as List<dynamic>?) ?? const [];
      // Only the primary call's failure blanks the screen; the engine being
      // unreachable is itself information and gets its own card.
      _error = results[0].ok ? null : results[0].error;
    });
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

    return RefreshIndicator(
      onRefresh: _refresh,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.md, Gap.lg, Gap.xxl),
        children: [
          _EngineCard(engine: _engine, controller: _controller),
          const SizedBox(height: Gap.md),
          _PositionsCard(status: _status),
          const SizedBox(height: Gap.md),
          _TodayTradesCard(trades: _todayTrades),
        ],
      ),
    );
  }
}

/// Engine state: score, zone, and whether an entry is currently permitted.
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
      title: slots.length == 1 ? 'Open position' : '${slots.length} open positions',
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
                    style: AppText.kicker.copyWith(color: scheme.onSurfaceVariant),
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

/// Trades closed today.
class _TodayTradesCard extends StatelessWidget {
  const _TodayTradesCard({required this.trades});

  final List<dynamic> trades;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final rows = trades.whereType<Map<String, dynamic>>().toList();
    final total = rows.fold<double>(
      0,
      (sum, row) => sum + ((row['pnl_usd'] as num?)?.toDouble() ?? 0),
    );

    return AppCard(
      kicker: "Today",
      title: rows.isEmpty ? 'No trades yet' : '${rows.length} closed',
      trailing: rows.isEmpty
          ? null
          : Text(
              _signed(total, 2),
              style: AppText.metric.copyWith(color: signedColour(total)),
            ),
      child: rows.isEmpty
          ? Text(
              'Nothing closed today.',
              style: AppText.body.copyWith(color: scheme.onSurfaceVariant),
            )
          : Column(
              children: [
                for (final row in rows.take(8))
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 5),
                    child: Row(
                      children: [
                        Expanded(
                          child: Text(
                            '${row['symbol'] ?? '—'}',
                            style: AppText.body,
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                        const SizedBox(width: Gap.sm),
                        Text(
                          _signed((row['pnl_usd'] as num?)?.toDouble() ?? 0, 2),
                          style: AppText.number.copyWith(
                            color: signedColour((row['pnl_usd'] as num?)),
                          ),
                        ),
                      ],
                    ),
                  ),
                if (rows.length > 8)
                  Padding(
                    padding: const EdgeInsets.only(top: Gap.sm),
                    child: Text(
                      '+ ${rows.length - 8} more on Performance',
                      style: AppText.caption
                          .copyWith(color: scheme.onSurfaceVariant),
                    ),
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
