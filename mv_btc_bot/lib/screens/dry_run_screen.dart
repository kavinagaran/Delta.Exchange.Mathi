library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../theme/design.dart';
import '../widgets/kit.dart';
import '../widgets/trade_controls.dart';

class DryRunScreen extends StatefulWidget {
  const DryRunScreen({
    super.key,
    required this.api,
    required this.onUnauthorised,
  });

  final DashboardApi api;
  final VoidCallback onUnauthorised;

  @override
  State<DryRunScreen> createState() => _DryRunScreenState();
}

class _DryRunScreenState extends State<DryRunScreen> {
  Map<String, dynamic>? _status;
  Map<String, dynamic>? _summary;
  Map<String, dynamic>? _controller;
  List<Map<String, dynamic>> _today = const [];
  List<Map<String, dynamic>> _history = const [];
  String? _error;
  bool _loading = true;
  bool _closing = false;
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    _refresh();
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
      widget.api.dryStatus(),
      widget.api.drySummary(),
      widget.api.dryTodayTrades(),
      widget.api.dryTrades(),
      widget.api.scoreAutoStatus(),
    ]);
    if (!mounted) return;
    if (results.any((result) => result.unauthorised)) {
      widget.onUnauthorised();
      return;
    }
    setState(() {
      _loading = false;
      _status = results[0].data as Map<String, dynamic>?;
      _summary = results[1].data as Map<String, dynamic>?;
      _today = _maps(results[2].data);
      _history = _maps(results[3].data);
      _controller = results[4].data as Map<String, dynamic>?;
      _error = results[0].ok ? null : results[0].error;
    });
  }

  Map<String, dynamic>? get _position {
    final direct = _status?['score_zone_position'];
    if (direct is Map<String, dynamic> &&
        '${direct['status']}'.toUpperCase() == 'OPEN') {
      return direct;
    }
    for (final row in _today) {
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
        title: const Text('Close dry-run trade?'),
        content: Text(
          '${trade['symbol'] ?? 'Current position'} · simulation only',
        ),
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
    final result = await widget.api.squareOff(
      slot: slot,
      targetMode: 'dry_run',
    );
    if (!mounted) return;
    setState(() => _closing = false);
    _message(
      result.ok ? 'Dry-run trade closed' : result.error ?? 'Close failed',
      result.ok,
    );
    if (result.ok) await _refresh(quiet: true);
  }

  void _message(String text, bool ok) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(text),
        backgroundColor: ok ? kPositive : kNegative,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_loading && _status == null) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    }
    if (_error != null && _status == null) {
      return StatePlaceholder(
        icon: Icons.cloud_off_rounded,
        message: 'Dry Run workspace unavailable',
        detail: _error,
        onRetry: _refresh,
        tone: kNegative,
      );
    }
    final position = _position;
    return RefreshIndicator(
      onRefresh: _refresh,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.md, Gap.lg, Gap.xxl),
        children: [
          _DryRunHero(summary: _summary, today: _today, controller: _controller),
          const SizedBox(height: Gap.md),
          if (position == null)
            const AppCard(
              kicker: 'Current trade',
              title: 'Flat',
              child: Text('Waiting for an eligible zone.', style: AppText.body),
            )
          else
            _DryRunPositionCard(
              trade: position,
              busy: _closing,
              onClose: () => _close(position),
              onProtection: () async {
                final saved = await showProtectionEditor(
                  context: context,
                  api: widget.api,
                  trade: position,
                );
                if (saved && mounted) {
                  _message('Protection updated', true);
                  await _refresh(quiet: true);
                }
              },
              onPayoff: () => showPayoffSheet(context, position),
            ),
          const SizedBox(height: Gap.md),
          _DryRunTradesCard(title: "Today's trades", rows: _today, limit: 12),
          const SizedBox(height: Gap.md),
          _DryRunTradesCard(title: 'History', rows: _history, limit: 60),
        ],
      ),
    );
  }
}

class _DryRunHero extends StatelessWidget {
  const _DryRunHero({
    required this.summary,
    required this.today,
    required this.controller,
  });

  final Map<String, dynamic>? summary;
  final List<Map<String, dynamic>> today;
  final Map<String, dynamic>? controller;

  @override
  Widget build(BuildContext context) {
    final total = _number(summary?['total_pnl']);
    final day = today.fold<double>(0, (sum, row) {
      final live =
          row['_live'] == true || '${row['status']}'.toUpperCase() == 'OPEN';
      return sum + (_number(live ? row['live_pnl'] : row['pnl_usd']) ?? 0);
    });
    final mode = '${controller?['mode'] ?? 'disabled'}'
        .replaceAll('_', ' ')
        .toUpperCase();
    return AppCard(
      kicker: 'Dry Run trading',
      title: 'Simulation workspace',
      accent: kWarning,
      trailing: StatusPill(
        mode,
        colour: mode == 'DRY RUN' ? kWarning : kNeutral,
      ),
      child: MetricWrap(
        children: [
          MetricTile(
            label: 'Today',
            value: _money(day),
            colour: signedColour(day),
          ),
          MetricTile(
            label: 'Total P&L',
            value: total == null ? '—' : _money(total),
            colour: signedColour(total),
          ),
          MetricTile(
            label: 'Win rate',
            value: summary?['win_rate'] == null
                ? '—'
                : '${_number(summary?['win_rate'])!.toStringAsFixed(1)}%',
          ),
          MetricTile(
            label: 'Trades',
            value:
                '${summary?['total_days'] ?? _number(summary?['trades'])?.toInt() ?? 0}',
          ),
        ],
      ),
    );
  }
}

class _DryRunPositionCard extends StatelessWidget {
  const _DryRunPositionCard({
    required this.trade,
    required this.busy,
    required this.onClose,
    required this.onProtection,
    required this.onPayoff,
  });

  final Map<String, dynamic> trade;
  final bool busy;
  final VoidCallback onClose;
  final VoidCallback onProtection;
  final VoidCallback onPayoff;

  @override
  Widget build(BuildContext context) {
    final pnl = _number(trade['live_pnl'] ?? trade['pnl_usd']);
    final zone = _zoneShort(trade);
    return AppCard(
      kicker: 'Current trade',
      title: '${trade['symbol'] ?? '—'}',
      accent: zoneColour(
        '${trade['trend_score_zone'] ?? trade['engine_zone'] ?? ''}',
      ),
      trailing: StatusPill('OPEN', colour: kWarning),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          MetricTile(
            label: '$zone · live P&L',
            value: pnl == null ? '—' : _money(pnl),
            colour: signedColour(pnl),
            big: true,
          ),
          const SizedBox(height: Gap.md),
          MetricWrap(
            children: [
              MetricTile(label: 'Lots', value: '${trade['lots'] ?? '—'}'),
              MetricTile(label: 'Entry', value: _price(trade['entry_mark'])),
              MetricTile(
                label: 'Mark',
                value: _price(trade['current_mark'] ?? trade['mark_price']),
              ),
              MetricTile(
                label: 'Side',
                value: '${trade['side'] ?? '—'}'.toUpperCase(),
              ),
            ],
          ),
          const SizedBox(height: Gap.lg),
          TradeActionBar(
            busy: busy,
            onClose: onClose,
            onProtection: onProtection,
            onPayoff: onPayoff,
          ),
        ],
      ),
    );
  }
}

class _DryRunTradesCard extends StatefulWidget {
  const _DryRunTradesCard({
    required this.title,
    required this.rows,
    required this.limit,
  });

  final String title;
  final List<Map<String, dynamic>> rows;
  final int limit;

  @override
  State<_DryRunTradesCard> createState() => _DryRunTradesCardState();
}

class _DryRunTradesCardState extends State<_DryRunTradesCard> {
  String _originFilter = '';

  // "Today" is the bot's trading day (Delta delists each day's contract at
  // 17:30 IST), not the IST calendar day — matches the web Today panel.
  static const _windowNote = 'Trading day: 5:31 pm IST → 5:30 pm IST next day';

  @override
  Widget build(BuildContext context) {
    // The API returns rows oldest-first; filter by origin first, then sort
    // newest-first before capping to `limit` so a long History list shows its
    // most recent matching trades instead of whatever entered first.
    final filtered = widget.rows
        .where((row) => originMatches(row, _originFilter))
        .toList()
      ..sort((a, b) => _entryMoment(b).compareTo(_entryMoment(a)));
    final shown = filtered.take(widget.limit).toList();
    final isToday = widget.title == "Today's trades";
    final onSurfaceVariant = Theme.of(context).colorScheme.onSurfaceVariant;
    return AppCard(
      kicker: 'Dry Run',
      title: '${widget.title} · ${widget.rows.length}',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          OriginFilterBar(
            value: _originFilter,
            onChanged: (value) => setState(() => _originFilter = value),
          ),
          const SizedBox(height: Gap.sm),
          if (isToday) ...[
            Text(
              _windowNote,
              style: AppText.caption.copyWith(color: onSurfaceVariant),
            ),
            const SizedBox(height: Gap.xs),
          ],
          if (shown.isEmpty)
            Text(
              widget.rows.isEmpty
                  ? 'No dry-run trades.'
                  : 'No trades match this filter.',
              style: AppText.body.copyWith(color: onSurfaceVariant),
            )
          else
            Column(
              children: [
                for (var index = 0; index < shown.length; index++) ...[
                  _TradeRow(trade: shown[index]),
                  if (index != shown.length - 1) const Divider(height: Gap.lg),
                ],
              ],
            ),
        ],
      ),
    );
  }
}

class _TradeRow extends StatelessWidget {
  const _TradeRow({required this.trade});

  final Map<String, dynamic> trade;

  @override
  Widget build(BuildContext context) {
    final live =
        trade['_live'] == true || '${trade['status']}'.toUpperCase() == 'OPEN';
    final pnl = _number(live ? trade['live_pnl'] : trade['pnl_usd']);
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if ('${trade['origin_label'] ?? ''}'.trim().isNotEmpty) ...[
          OriginChip.forTrade(trade),
          const SizedBox(width: Gap.sm),
        ],
        StatusPill(
          _zoneShort(trade),
          colour: zoneColour(
            '${trade['trend_score_zone'] ?? trade['engine_zone'] ?? ''}',
          ),
          dot: false,
        ),
        const SizedBox(width: Gap.sm),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                '${trade['symbol'] ?? '—'}',
                style: AppText.body,
                overflow: TextOverflow.ellipsis,
              ),
              Text(
                '${trade['lots'] ?? '—'} lots · ${live ? 'OPEN' : 'CLOSED'}',
                style: AppText.caption.copyWith(
                  color: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
              ),
            ],
          ),
        ),
        Text(
          pnl == null ? '—' : _money(pnl),
          style: AppText.number.copyWith(color: signedColour(pnl)),
        ),
      ],
    );
  }
}

List<Map<String, dynamic>> _maps(Object? value) =>
    (value as List<dynamic>? ?? const [])
        .whereType<Map<String, dynamic>>()
        .toList();

/// UTC instant a trade opened. entry_date/entry_time(_utc) are always
/// stored in UTC; an unparseable or missing stamp sorts to the epoch rather
/// than throwing, so a malformed legacy row doesn't crash the list.
DateTime _entryMoment(Map<String, dynamic> trade) {
  final date = '${trade['entry_date'] ?? trade['date'] ?? ''}'.trim();
  if (date.isEmpty) return DateTime.fromMillisecondsSinceEpoch(0, isUtc: true);
  final clock = '${trade['entry_time_utc'] ?? trade['entry_time'] ?? '00:00:00'}'
      .trim()
      .replaceAll('Z', '');
  return DateTime.tryParse('${date}T${clock}Z')?.toUtc() ??
      DateTime.fromMillisecondsSinceEpoch(0, isUtc: true);
}

double? _number(Object? value) =>
    value is num ? value.toDouble() : double.tryParse('$value');

String _money(double value) =>
    '${value > 0 ? '+' : ''}\$${value.toStringAsFixed(2)}';

String _price(Object? value) {
  final number = _number(value);
  return number == null ? '—' : '\$${number.toStringAsFixed(2)}';
}

String _zoneShort(Map<String, dynamic> trade) {
  final zone = '${trade['trend_score_zone'] ?? trade['engine_zone'] ?? ''}'
      .toUpperCase();
  if (zone.startsWith('CE') || '${trade['symbol']}'.startsWith('C-')) {
    return 'CE';
  }
  if (zone.startsWith('PE') || '${trade['symbol']}'.startsWith('P-')) {
    return 'PE';
  }
  if (zone == 'SHORT_MOVE' || '${trade['symbol']}'.startsWith('MV-')) {
    return 'MV';
  }
  return 'TRADE';
}
