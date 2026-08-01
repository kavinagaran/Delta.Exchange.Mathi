/// Performance — realised results, an equity curve, and the trade list.
///
/// Native because the web version is a wide table plus a Chart.js canvas, and
/// both are poor on a phone. The curve here is painted directly, which also
/// removes a JavaScript chart library from the mobile path entirely.
library;

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
  Map<String, dynamic>? _summary;
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

    final results = await Future.wait([
      widget.api.summary(),
      widget.api.trades(),
    ]);
    if (!mounted) return;
    if (results.any((r) => r.unauthorised)) {
      widget.onUnauthorised();
      return;
    }

    setState(() {
      _loading = false;
      _summary = results[0].data as Map<String, dynamic>?;
      _trades = ((results[1].data as List<dynamic>?) ?? const [])
          .whereType<Map<String, dynamic>>()
          .where((t) => t['pnl_usd'] != null)
          .toList();
      _error = results[1].ok ? null : results[1].error;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (_loading && _trades.isEmpty && _summary == null) {
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

    // `_pnl_stats` returns {} when there are no closed trades, so an empty
    // summary means "nothing yet", not "failed".
    final hasStats = _summary != null && _summary!.isNotEmpty;

    return RefreshIndicator(
      onRefresh: _refresh,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.md, Gap.lg, Gap.xxl),
        children: [
          if (!hasStats)
            const AppCard(
              kicker: 'Performance',
              title: 'No closed trades yet',
              child: Text(
                'Statistics appear once a position has been closed.',
                style: AppText.body,
              ),
            )
          else ...[
            _SummaryCard(summary: _summary!),
            const SizedBox(height: Gap.md),
            _EquityCard(trades: _trades),
            const SizedBox(height: Gap.md),
            _TradeListCard(trades: _trades),
          ],
        ],
      ),
    );
  }
}

class _SummaryCard extends StatelessWidget {
  const _SummaryCard({required this.summary});

  final Map<String, dynamic> summary;

  @override
  Widget build(BuildContext context) {
    double? num_(String key) => (summary[key] as num?)?.toDouble();
    final total = num_('total_pnl');
    final winRate = num_('win_rate');
    final wins = summary['wins'];
    final losses = summary['losses'];

    return AppCard(
      kicker: 'Performance',
      title: 'Realised',
      accent: signedColour(total),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          MetricTile(
            label: 'Total P&L',
            value: total == null ? '—' : _money(total),
            colour: signedColour(total),
            sub: '${summary['total_days'] ?? 0} closed trades',
            big: true,
          ),
          const SizedBox(height: Gap.lg),
          Row(
            children: [
              Expanded(
                child: MetricTile(
                  label: 'Win rate',
                  value: winRate == null
                      ? '—'
                      : '${winRate.toStringAsFixed(1)}%',
                  sub: '$wins W · $losses L',
                ),
              ),
              Expanded(
                child: MetricTile(
                  label: 'Reward : risk',
                  value: num_('rr')?.toStringAsFixed(2) ?? '—',
                ),
              ),
            ],
          ),
          const SizedBox(height: Gap.md),
          StatRow(
            'Average win',
            num_('avg_win') == null ? '—' : _money(num_('avg_win')!),
            valueColour: kPositive,
          ),
          StatRow(
            'Average loss',
            num_('avg_loss') == null ? '—' : _money(num_('avg_loss')!),
            valueColour: kNegative,
          ),
          StatRow(
            'Max drawdown',
            num_('max_dd') == null ? '—' : _money(num_('max_dd')!),
            valueColour: kNegative,
          ),
        ],
      ),
    );
  }
}

class _EquityCard extends StatelessWidget {
  const _EquityCard({required this.trades});

  final List<Map<String, dynamic>> trades;

  @override
  Widget build(BuildContext context) {
    // Oldest first, so the curve reads left to right in time order. The API
    // returns newest-first for the table.
    final ordered = trades.reversed
        .map((t) => (t['pnl_usd'] as num?)?.toDouble() ?? 0)
        .toList();
    if (ordered.length < 2) return const SizedBox.shrink();

    var running = 0.0;
    final cumulative = <double>[];
    for (final pnl in ordered) {
      running += pnl;
      cumulative.add(running);
    }

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
    final shown = trades.take(40).toList();

    return AppCard(
      kicker: 'History',
      title: '${trades.length} closed trades',
      padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.lg, Gap.lg, Gap.sm),
      child: Column(
        children: [
          for (final trade in shown)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 7),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
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
                          ].where((v) => v != null).join(' · '),
                          style: AppText.caption.copyWith(
                            color: scheme.onSurfaceVariant,
                          ),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(width: Gap.sm),
                  Text(
                    _money((trade['pnl_usd'] as num).toDouble()),
                    style: AppText.number.copyWith(
                      color: signedColour(trade['pnl_usd'] as num),
                    ),
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

String _money(double value) {
  final sign = value > 0 ? '+' : '';
  return '$sign\$${value.toStringAsFixed(2)}';
}
