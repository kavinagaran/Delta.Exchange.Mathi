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

class _TradeListCard extends StatelessWidget {
  const _TradeListCard({required this.trades});

  final List<Map<String, dynamic>> trades;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final shown = trades.reversed.take(40).toList();

    return AppCard(
      kicker: 'History',
      title: '${trades.length} trade cycles',
      padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.lg, Gap.lg, Gap.sm),
      child: Column(
        children: [
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
          if (trades.length > shown.length)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: Gap.sm),
              child: Text(
                '+ ${trades.length - shown.length} older — open Performance '
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
