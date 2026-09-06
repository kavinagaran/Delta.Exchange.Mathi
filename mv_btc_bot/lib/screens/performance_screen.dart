/// Performance — realised results, an equity curve, and the trade list.
///
/// Native because the web version is a wide table plus a Chart.js canvas, and
/// both are poor on a phone. The curve here is painted directly, which also
/// removes a JavaScript chart library from the mobile path entirely.
library;

import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../theme/design.dart';
import '../widgets/kit.dart';

class PerformanceScreen extends StatefulWidget {
  const PerformanceScreen({
    super.key,
    required this.api,
    required this.onUnauthorised,
  });

  final DashboardApi api;
  final VoidCallback onUnauthorised;

  @override
  State<PerformanceScreen> createState() => _PerformanceScreenState();
}

class _PerformanceScreenState extends State<PerformanceScreen> {
  List<Map<String, dynamic>> _trades = const [];
  String? _error;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  Future<void> _refresh() async {
    if (mounted) setState(() => _loading = true);

    final result = await widget.api.performanceTrades();
    if (!mounted) return;
    if (result.unauthorised) {
      widget.onUnauthorised();
      return;
    }

    setState(() {
      _loading = false;
      _trades = (result.data ?? const [])
          .whereType<Map<String, dynamic>>()
          .toList();
      _error = result.ok ? null : result.error;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (_loading && _trades.isEmpty) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    }
    if (_error != null && _trades.isEmpty) {
      return StatePlaceholder(
        icon: Icons.cloud_off_rounded,
        message: 'Cannot load performance',
        detail: _error,
        onRetry: _refresh,
        tone: kNegative,
      );
    }

    final stats = _PerformanceStats.fromTrades(_trades);

    return RefreshIndicator(
      onRefresh: _refresh,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.md, Gap.lg, Gap.xxl),
        children: [
          if (_trades.isEmpty)
            const AppCard(
              kicker: 'Performance',
              title: 'No exchange trades yet',
              child: Text(
                'Delta Exchange trade history will appear here.',
                style: AppText.body,
              ),
            )
          else ...[
            _SummaryCard(stats: stats),
            const SizedBox(height: Gap.md),
            _DailyPnlCard(stats: stats),
            const SizedBox(height: Gap.md),
            _EquityCard(stats: stats),
            const SizedBox(height: Gap.md),
            _TradeListCard(trades: _trades),
          ],
        ],
      ),
    );
  }
}

class _SummaryCard extends StatelessWidget {
  const _SummaryCard({required this.stats});

  final _PerformanceStats stats;

  @override
  Widget build(BuildContext context) {
    return AppCard(
      kicker: 'Delta Exchange',
      title: 'Real-trade performance',
      accent: signedColour(stats.netPnl),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          MetricTile(
            label: 'Total P&L',
            value: stats.valued == 0 ? '—' : _money(stats.netPnl),
            colour: signedColour(stats.valued == 0 ? null : stats.netPnl),
            sub: '${stats.valued}/${stats.closed} closed trades valued',
            big: true,
          ),
          const SizedBox(height: Gap.lg),
          Row(
            children: [
              Expanded(
                child: MetricTile(
                  label: 'Win rate',
                  value: stats.winRate == null
                      ? '—'
                      : '${stats.winRate!.toStringAsFixed(1)}%',
                  sub: '${stats.winners} W · ${stats.losers} L',
                ),
              ),
              Expanded(
                child: MetricTile(
                  label: 'Risk : reward',
                  value: stats.rewardRisk == null
                      ? '—'
                      : '1 : ${stats.rewardRisk!.toStringAsFixed(2)}',
                ),
              ),
            ],
          ),
          const SizedBox(height: Gap.md),
          StatRow(
            'Average win',
            stats.averageWin == null ? '—' : _money(stats.averageWin!),
            valueColour: kPositive,
          ),
          StatRow(
            'Average loss',
            stats.averageLoss == null ? '—' : _money(stats.averageLoss!),
            valueColour: kNegative,
          ),
          StatRow(
            'Max drawdown',
            stats.valued == 0 ? '—' : _lossMoney(stats.maxDrawdown),
            valueColour: kNegative,
          ),
          StatRow(
            'Gross P&L',
            stats.grossValued == 0 ? '—' : _money(stats.grossPnl),
            valueColour: signedColour(
              stats.grossValued == 0 ? null : stats.grossPnl,
            ),
          ),
          StatRow('Fees & charges', stats.feeLabel, valueColour: kWarning),
          StatRow('Trade cycles', '${stats.total} · ${stats.open} open'),
        ],
      ),
    );
  }
}

class _EquityCard extends StatelessWidget {
  const _EquityCard({required this.stats});

  final _PerformanceStats stats;

  @override
  Widget build(BuildContext context) {
    final cumulative = stats.equity;
    if (cumulative.length < 2) return const SizedBox.shrink();

    return AppCard(
      kicker: 'Equity curve',
      title: 'Cumulative P&L',
      trailing: Text(
        _money(cumulative.last),
        style: AppText.number.copyWith(color: signedColour(cumulative.last)),
      ),
      child: SizedBox(
        height: 132,
        child: CustomPaint(
          painter: _EquityPainter(
            values: cumulative,
            line: signedColour(cumulative.last),
            grid: Theme.of(context).colorScheme.outline,
          ),
          size: Size.infinite,
        ),
      ),
    );
  }
}

class _DailyPnlCard extends StatelessWidget {
  const _DailyPnlCard({required this.stats});

  final _PerformanceStats stats;

  @override
  Widget build(BuildContext context) {
    if (stats.dailyPnl.isEmpty) return const SizedBox.shrink();
    final cumulative = stats.dailyPnl.fold<double>(
      0,
      (total, point) => total + point.pnl,
    );
    return AppCard(
      kicker: 'Daily performance',
      title: 'Daily P/L — day-wise',
      trailing: Text(
        _money(cumulative),
        style: AppText.number.copyWith(color: signedColour(cumulative)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              _ChartLegend(
                label: 'Daily net P&L',
                colour: kPositive,
                bar: true,
              ),
              const SizedBox(width: Gap.md),
              _ChartLegend(label: 'Cumulative net P&L', colour: kWarning),
            ],
          ),
          const SizedBox(height: Gap.md),
          SizedBox(
            height: 158,
            child: CustomPaint(
              painter: _DailyPnlPainter(
                points: stats.dailyPnl,
                grid: Theme.of(context).colorScheme.outline,
              ),
              size: Size.infinite,
            ),
          ),
        ],
      ),
    );
  }
}

class _ChartLegend extends StatelessWidget {
  const _ChartLegend({
    required this.label,
    required this.colour,
    this.bar = false,
  });

  final String label;
  final Color colour;
  final bool bar;

  @override
  Widget build(BuildContext context) => Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      Container(
        width: 18,
        height: bar ? 10 : 3,
        decoration: BoxDecoration(
          color: colour.withValues(alpha: bar ? .45 : 1),
          border: bar ? Border.all(color: colour) : null,
          borderRadius: BorderRadius.circular(2),
        ),
      ),
      const SizedBox(width: 5),
      Text(label, style: AppText.caption),
    ],
  );
}

/// The compact native counterpart of the web's Dry Run graph: day-wise P&L
/// columns on the left scale and an amber cumulative path on its own scale.
class _DailyPnlPainter extends CustomPainter {
  const _DailyPnlPainter({required this.points, required this.grid});

  final List<({String day, double pnl})> points;
  final Color grid;

  @override
  void paint(Canvas canvas, Size size) {
    if (points.isEmpty) return;
    final daily = points.map((point) => point.pnl).toList();
    final cumulative = <double>[];
    var running = 0.0;
    for (final value in daily) {
      running += value;
      cumulative.add(running);
    }
    double plotY(double value, List<double> values) {
      final lowest = values.reduce(math.min);
      final highest = values.reduce(math.max);
      final low = lowest < 0 ? lowest : 0.0;
      final high = highest > 0 ? highest : 0.0;
      final span = (high - low).abs() < 1e-9 ? 1.0 : high - low;
      final pad = span * .14;
      return size.height -
          ((value - low + pad) / (span + pad * 2)) * size.height;
    }

    final dailyZero = plotY(0, daily);
    double x(int index) => points.length == 1
        ? size.width / 2
        : index / (points.length - 1) * size.width;

    canvas.drawLine(
      Offset(0, dailyZero),
      Offset(size.width, dailyZero),
      Paint()
        ..color = grid.withValues(alpha: .62)
        ..strokeWidth = 1,
    );

    final barWidth = math.max(
      5.0,
      math.min(22.0, size.width / (points.length * 1.8)),
    );
    for (var index = 0; index < daily.length; index++) {
      final value = daily[index];
      final y = plotY(value, daily);
      final top = math.min(y, dailyZero);
      final height = math.max(1.0, (y - dailyZero).abs());
      final colour = value >= 0 ? kPositive : kNegative;
      final rect = RRect.fromRectAndRadius(
        Rect.fromLTWH(x(index) - barWidth / 2, top, barWidth, height),
        const Radius.circular(3),
      );
      canvas.drawRRect(rect, Paint()..color = colour.withValues(alpha: .42));
      canvas.drawRRect(
        rect,
        Paint()
          ..color = colour
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1,
      );
    }

    if (points.length > 1) {
      final depth = Path()..moveTo(x(0), plotY(cumulative.first, cumulative));
      for (var index = 1; index < cumulative.length; index++) {
        depth.lineTo(x(index), plotY(cumulative[index], cumulative));
      }
      canvas.save();
      canvas.translate(3, 5);
      canvas.drawPath(
        depth,
        Paint()
          ..color = const Color(0xD9091728)
          ..strokeWidth = 7
          ..style = PaintingStyle.stroke
          ..strokeJoin = StrokeJoin.round
          ..strokeCap = StrokeCap.round,
      );
      canvas.restore();
    }

    for (var index = 1; index < cumulative.length; index++) {
      canvas.drawLine(
        Offset(x(index - 1), plotY(cumulative[index - 1], cumulative)),
        Offset(x(index), plotY(cumulative[index], cumulative)),
        Paint()
          ..color = kWarning
          ..strokeWidth = 3
          ..strokeCap = StrokeCap.round,
      );
    }
    for (var index = 0; index < cumulative.length; index++) {
      final point = Offset(x(index), plotY(cumulative[index], cumulative));
      canvas.drawCircle(
        point.translate(1.5, 2.5),
        4.5,
        Paint()..color = const Color(0xC9091728),
      );
      canvas.drawCircle(point, 3.5, Paint()..color = kWarning);
      canvas.drawCircle(point, 1.6, Paint()..color = const Color(0xFFE5FBFF));
    }
  }

  @override
  bool shouldRepaint(_DailyPnlPainter old) =>
      old.points != points || old.grid != grid;
}

/// Cumulative P&L with the drawdown from peak shaded underneath.
///
/// The zero line is always drawn, and the scale always includes zero: a curve
/// auto-fitted to a purely losing stretch would otherwise look like a rising
/// line, which is exactly backwards.
class _EquityPainter extends CustomPainter {
  const _EquityPainter({
    required this.values,
    required this.line,
    required this.grid,
  });

  final List<double> values;
  final Color line;
  final Color grid;

  @override
  void paint(Canvas canvas, Size size) {
    if (values.length < 2) return;

    final lowest = values.reduce((a, b) => a < b ? a : b);
    final highest = values.reduce((a, b) => a > b ? a : b);
    final low = lowest < 0 ? lowest : 0.0;
    final high = highest > 0 ? highest : 0.0;
    final span = (high - low).abs() < 1e-9 ? 1.0 : high - low;
    final pad = span * .12;

    double y(double value) =>
        size.height - ((value - low + pad) / (span + pad * 2)) * size.height;
    double x(int index) => index / (values.length - 1) * size.width;

    // Zero reference.
    final zeroY = y(0);
    canvas.drawLine(
      Offset(0, zeroY),
      Offset(size.width, zeroY),
      Paint()
        ..color = grid
        ..strokeWidth = 1,
    );

    final path = Path()..moveTo(x(0), y(values.first));
    for (var i = 1; i < values.length; i++) {
      path.lineTo(x(i), y(values[i]));
    }

    // Fill between the curve and zero, so time spent underwater is visible as
    // area rather than having to be inferred from the line's position.
    final fill = Path.from(path)
      ..lineTo(size.width, zeroY)
      ..lineTo(0, zeroY)
      ..close();
    canvas.drawPath(
      fill,
      Paint()
        ..shader = LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [line.withValues(alpha: .28), line.withValues(alpha: .02)],
        ).createShader(Rect.fromLTWH(0, 0, size.width, size.height)),
    );

    canvas.drawPath(
      path,
      Paint()
        ..color = line
        ..strokeWidth = 2
        ..style = PaintingStyle.stroke
        ..strokeJoin = StrokeJoin.round,
    );

    // Head marker.
    canvas.drawCircle(
      Offset(x(values.length - 1), y(values.last)),
      3.5,
      Paint()..color = line,
    );
  }

  @override
  bool shouldRepaint(_EquityPainter old) =>
      old.values != values || old.line != line;
}

class _TradeListCard extends StatefulWidget {
  const _TradeListCard({required this.trades});

  final List<Map<String, dynamic>> trades;

  @override
  State<_TradeListCard> createState() => _TradeListCardState();
}

class _TradeListCardState extends State<_TradeListCard> {
  String _originFilter = '';

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final filtered = widget.trades
        .where((trade) => originMatches(trade, _originFilter))
        .toList();
    final shown = filtered.reversed.take(40).toList();

    return AppCard(
      kicker: 'History',
      title: '${widget.trades.length} trade cycles',
      padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.lg, Gap.lg, Gap.sm),
      child: Column(
        children: [
          Align(
            alignment: Alignment.centerLeft,
            child: OriginFilterBar(
              value: _originFilter,
              onChanged: (value) => setState(() => _originFilter = value),
            ),
          ),
          const SizedBox(height: Gap.sm),
          if (shown.isEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: Gap.sm),
              child: Text(
                'No trades match this filter.',
                style: AppText.caption.copyWith(color: scheme.onSurfaceVariant),
              ),
            ),
          for (final trade in shown)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 7),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if ('${trade['origin_label'] ?? ''}'.trim().isNotEmpty) ...[
                    OriginChip.forTrade(trade),
                    const SizedBox(width: Gap.sm),
                  ],
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
                          [
                            trade['date'],
                            trade['side'],
                            trade['status'],
                          ].where((v) => v != null).join(' · '),
                          style: AppText.caption.copyWith(
                            color: scheme.onSurfaceVariant,
                          ),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(width: Gap.sm),
                  if (_number(trade['net_pnl_usd']) case final pnl?)
                    Text(
                      _money(pnl),
                      style: AppText.number.copyWith(color: signedColour(pnl)),
                    )
                  else
                    Text(
                      trade['status'] == 'OPEN' ? 'OPEN' : '—',
                      style: AppText.caption.copyWith(color: kWarning),
                    ),
                ],
              ),
            ),
          if (filtered.length > shown.length)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: Gap.sm),
              child: Text(
                '+ ${filtered.length - shown.length} older — open Performance '
                'on the web for the full ledger',
                textAlign: TextAlign.center,
                style: AppText.caption.copyWith(color: scheme.onSurfaceVariant),
              ),
            ),
        ],
      ),
    );
  }
}

class _PerformanceStats {
  const _PerformanceStats({
    required this.total,
    required this.open,
    required this.closed,
    required this.valued,
    required this.grossValued,
    required this.winners,
    required this.losers,
    required this.netPnl,
    required this.grossPnl,
    required this.averageWin,
    required this.averageLoss,
    required this.rewardRisk,
    required this.winRate,
    required this.maxDrawdown,
    required this.equity,
    required this.dailyPnl,
    required this.fees,
  });

  factory _PerformanceStats.fromTrades(List<Map<String, dynamic>> trades) {
    final closed = trades
        .where((trade) => '${trade['status']}'.toUpperCase() == 'CLOSED')
        .toList();
    final valued = <({double pnl, int order, double closedAt})>[];
    var grossPnl = 0.0;
    var grossValued = 0;
    final fees = <String, double>{};

    for (var index = 0; index < trades.length; index++) {
      final trade = trades[index];
      final isClosed = '${trade['status']}'.toUpperCase() == 'CLOSED';
      final net = _number(trade['net_pnl_usd']);
      if (isClosed && net != null) {
        final parsed = DateTime.tryParse('${trade['exit_at_utc'] ?? ''}');
        final sortTimestamp = _number(trade['sort_timestamp']);
        valued.add((
          pnl: net,
          order: index,
          closedAt:
              parsed?.millisecondsSinceEpoch.toDouble() ??
              (sortTimestamp == null ? index.toDouble() : sortTimestamp * 1000),
        ));
      }
      final gross = _number(trade['gross_pnl_usd']);
      if (isClosed && gross != null) {
        grossPnl += gross;
        grossValued++;
      }
      final tradeFees = trade['fees'];
      if (tradeFees is List) {
        for (final fee in tradeFees.whereType<Map>()) {
          final amount = _number(fee['amount']);
          if (amount == null) continue;
          final asset = '${fee['asset'] ?? 'fee units'}';
          fees[asset] = (fees[asset] ?? 0) + amount;
        }
      }
    }

    final pnlValues = valued.map((item) => item.pnl).toList();
    final wins = pnlValues.where((pnl) => pnl > 0).toList();
    final losses = pnlValues.where((pnl) => pnl < 0).toList();
    final netPnl = pnlValues.fold<double>(0, (sum, pnl) => sum + pnl);
    final winTotal = wins.fold<double>(0, (sum, pnl) => sum + pnl);
    final lossTotal = losses.fold<double>(0, (sum, pnl) => sum + pnl);
    final averageWin = wins.isEmpty ? null : winTotal / wins.length;
    final averageLoss = losses.isEmpty ? null : lossTotal / losses.length;
    final rewardRisk = averageWin == null || averageLoss == null
        ? null
        : averageWin / averageLoss.abs();

    valued.sort(
      (left, right) => left.closedAt.compareTo(right.closedAt) != 0
          ? left.closedAt.compareTo(right.closedAt)
          : left.order.compareTo(right.order),
    );
    var running = 0.0;
    var peak = 0.0;
    var maxDrawdown = 0.0;
    final equity = <double>[];
    for (final trade in valued) {
      running += trade.pnl;
      equity.add(running);
      peak = math.max(peak, running);
      maxDrawdown = math.max(maxDrawdown, peak - running);
    }
    final dailyTotals = <String, double>{};
    for (final trade in valued) {
      final rawDate = '${trades[trade.order]['date'] ?? ''}'.trim();
      final date = rawDate.isNotEmpty
          ? rawDate
          : DateTime.fromMillisecondsSinceEpoch(
              trade.closedAt.toInt(),
              isUtc: true,
            ).toIso8601String().substring(0, 10);
      dailyTotals[date] = (dailyTotals[date] ?? 0) + trade.pnl;
    }
    final dailyPnl =
        dailyTotals.entries
            .map((entry) => (day: entry.key, pnl: entry.value))
            .toList()
          ..sort((left, right) => left.day.compareTo(right.day));

    return _PerformanceStats(
      total: trades.length,
      open: trades.length - closed.length,
      closed: closed.length,
      valued: valued.length,
      grossValued: grossValued,
      winners: wins.length,
      losers: losses.length,
      netPnl: netPnl,
      grossPnl: grossPnl,
      averageWin: averageWin,
      averageLoss: averageLoss,
      rewardRisk: rewardRisk,
      winRate: valued.isEmpty ? null : wins.length / valued.length * 100,
      maxDrawdown: maxDrawdown,
      equity: equity,
      dailyPnl: dailyPnl,
      fees: fees,
    );
  }

  final int total;
  final int open;
  final int closed;
  final int valued;
  final int grossValued;
  final int winners;
  final int losers;
  final double netPnl;
  final double grossPnl;
  final double? averageWin;
  final double? averageLoss;
  final double? rewardRisk;
  final double? winRate;
  final double maxDrawdown;
  final List<double> equity;
  final List<({String day, double pnl})> dailyPnl;
  final Map<String, double> fees;

  String get feeLabel {
    if (fees.isEmpty) return 'Not reported';
    return fees.entries
        .map((entry) {
          if (entry.key.toUpperCase() == 'USD') {
            return '\$${entry.value.toStringAsFixed(2)}';
          }
          return '${entry.value.toStringAsFixed(4)} ${entry.key}';
        })
        .join(' · ');
  }
}

double? _number(Object? value) {
  if (value is num) return value.toDouble();
  return double.tryParse('$value');
}

String _money(double value) {
  final sign = value > 0 ? '+' : '';
  return '$sign\$${value.toStringAsFixed(2)}';
}

String _lossMoney(double value) =>
    value <= 0 ? '\$0.00' : '-\$${value.toStringAsFixed(2)}';
