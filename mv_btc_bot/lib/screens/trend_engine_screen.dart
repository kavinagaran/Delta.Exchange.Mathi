library;

import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../theme/design.dart';
import '../widgets/kit.dart';

class TrendEngineScreen extends StatefulWidget {
  const TrendEngineScreen({
    super.key,
    required this.api,
    required this.onUnauthorised,
  });

  final DashboardApi api;
  final VoidCallback onUnauthorised;

  @override
  State<TrendEngineScreen> createState() => _TrendEngineScreenState();
}

class _TrendEngineScreenState extends State<TrendEngineScreen> {
  Map<String, dynamic>? _snapshot;
  Map<String, dynamic>? _live;
  Map<String, dynamic>? _status;
  Map<String, dynamic>? _history;
  String? _error;
  bool _loading = true;
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    _refresh();
    _poll = Timer.periodic(
      const Duration(seconds: 5),
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
      widget.api.engineSnapshot(),
      widget.api.engineLive(),
      widget.api.engineStatus(),
      widget.api.decisionHistory(),
    ]);
    if (!mounted) return;
    if (results.any((result) => result.unauthorised)) {
      widget.onUnauthorised();
      return;
    }
    setState(() {
      _loading = false;
      _snapshot = results[0].data;
      _live = results[1].data;
      _status = results[2].data;
      _history = results[3].data;
      _error = results[0].ok ? null : results[0].error;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (_loading && _snapshot == null) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    }
    if (_snapshot == null) {
      return StatePlaceholder(
        icon: Icons.insights_rounded,
        message: 'Trend Engine unavailable',
        detail: _error,
        onRetry: _refresh,
        tone: kNegative,
      );
    }
    final snapshot = _snapshot!;
    final components = _mapList(snapshot['components']);
    final timeframes = _mapList(snapshot['timeframes']);
    final gates = _mapList(snapshot['gates']);
    final reasons = (snapshot['reason_codes'] as List<dynamic>? ?? const [])
        .map((value) => '$value')
        .toList();
    return RefreshIndicator(
      onRefresh: _refresh,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.md, Gap.lg, Gap.xxl),
        children: [
          _DecisionHero(snapshot: snapshot, live: _live),
          const SizedBox(height: Gap.md),
          _DecisionChart(history: _history),
          const SizedBox(height: Gap.md),
          _ComponentsCard(components: components),
          const SizedBox(height: Gap.md),
          _TimeframesCard(timeframes: timeframes),
          const SizedBox(height: Gap.md),
          _GatesCard(gates: gates),
          const SizedBox(height: Gap.md),
          _ReasonCard(reasons: reasons),
          const SizedBox(height: Gap.md),
          _EngineStatusCard(status: _status, snapshot: snapshot),
        ],
      ),
    );
  }
}

class _DecisionHero extends StatelessWidget {
  const _DecisionHero({required this.snapshot, required this.live});

  final Map<String, dynamic> snapshot;
  final Map<String, dynamic>? live;

  @override
  Widget build(BuildContext context) {
    final committed = _number(snapshot['trend_score']);
    final preview = _number(live?['trend_score'] ?? live?['score']);
    final zone = '${snapshot['zone'] ?? 'HOLD'}';
    final quality = '${snapshot['data_quality'] ?? 'UNKNOWN'}';
    final confidence = _number(snapshot['confidence']);
    final regime = _regimeLabel('${snapshot['regime'] ?? 'DEGRADED'}');
    return AppCard(
      kicker: 'BTC Trend Engine',
      title: regime,
      accent: zoneColour(zone),
      trailing: StatusPill(
        quality == 'OK' ? 'LIVE' : quality.replaceAll('_', ' '),
        colour: quality == 'OK' ? kPositive : kNegative,
      ),
      child: Column(
        children: [
          Row(
            children: [
              Expanded(
                child: _ScoreDial(
                  label: 'Preview',
                  score: preview,
                  zone: _zoneForScore(preview),
                ),
              ),
              const SizedBox(width: Gap.md),
              Expanded(
                child: _ScoreDial(
                  label: 'Committed',
                  score: committed,
                  zone: zone,
                ),
              ),
            ],
          ),
          const SizedBox(height: Gap.md),
          ScoreMeter(score: committed ?? 0),
          const SizedBox(height: Gap.sm),
          Row(
            children: [
              Expanded(
                child: StatusPill(
                  _zoneLabel(zone),
                  colour: zoneColour(zone),
                  dot: false,
                ),
              ),
              const SizedBox(width: Gap.sm),
              StatusPill(
                confidence == null
                    ? '—'
                    : '${(confidence * 100).round()}% CONF',
                colour: kNeutral,
                dot: false,
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _ScoreDial extends StatelessWidget {
  const _ScoreDial({
    required this.label,
    required this.score,
    required this.zone,
  });
  final String label;
  final double? score;
  final String zone;

  @override
  Widget build(BuildContext context) {
    final colour = zoneColour(zone);
    return AspectRatio(
      aspectRatio: 1,
      child: Stack(
        fit: StackFit.expand,
        children: [
          CircularProgressIndicator(
            value: score == null
                ? 0
                : (score!.abs() / 100).clamp(0, 1).toDouble(),
            strokeWidth: 9,
            backgroundColor: Theme.of(
              context,
            ).colorScheme.surfaceContainerHighest,
            color: colour,
            strokeCap: StrokeCap.round,
          ),
          Center(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  label.toUpperCase(),
                  style: AppText.kicker.copyWith(
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                    fontSize: 7.5,
                  ),
                ),
                const SizedBox(height: 3),
                Text(
                  score == null ? '—' : _signed(score!, 1),
                  style: AppText.metric.copyWith(color: colour, fontSize: 24),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _DecisionChart extends StatelessWidget {
  const _DecisionChart({required this.history});
  final Map<String, dynamic>? history;

  @override
  Widget build(BuildContext context) {
    final points = _mapList(history?['decisions'])
        .map((item) => _number(item['committed_score']))
        .whereType<double>()
        .toList();
    final markers = _mapList(history?['trade_markers']);
    return AppCard(
      kicker: '24H · 5M',
      title: 'Committed score',
      trailing: StatusPill('${points.length} points', dot: false),
      child: points.length < 2
          ? const StatePlaceholder(
              icon: Icons.show_chart_rounded,
              message: 'Collecting decisions…',
            )
          : SizedBox(
              height: 240,
              child: CustomPaint(
                painter: _ScoreChartPainter(
                  values: points,
                  markerCount: markers.length,
                  grid: Theme.of(context).colorScheme.outline,
                  label: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
                size: Size.infinite,
              ),
            ),
    );
  }
}

class _ScoreChartPainter extends CustomPainter {
  const _ScoreChartPainter({
    required this.values,
    required this.markerCount,
    required this.grid,
    required this.label,
  });
  final List<double> values;
  final int markerCount;
  final Color grid;
  final Color label;

  @override
  void paint(Canvas canvas, Size size) {
    const left = 30.0;
    const top = 8.0;
    const bottom = 20.0;
    final plot = Rect.fromLTRB(left, top, size.width, size.height - bottom);
    final minValue = values.reduce(math.min);
    final maxValue = values.reduce(math.max);
    final low = math.max(-100.0, math.min(minValue - 10, -30.0)).toDouble();
    final high = math.min(100.0, math.max(maxValue + 10, 30.0)).toDouble();
    double y(double value) =>
        plot.bottom - (value - low) / (high - low) * plot.height;
    double x(int index) => plot.left + index / (values.length - 1) * plot.width;

    final zones = <(double, double, Color)>[
      (math.max(40.0, low).toDouble(), high, kZoneCall),
      (
        math.max(30.0, low).toDouble(),
        math.min(40.0, high).toDouble(),
        kZoneHold,
      ),
      (
        math.max(-30.0, low).toDouble(),
        math.min(30.0, high).toDouble(),
        kZoneMove,
      ),
      (
        math.max(-40.0, low).toDouble(),
        math.min(-30.0, high).toDouble(),
        kZoneHold,
      ),
      (low, math.min(-40.0, high).toDouble(), kZonePut),
    ];
    for (final zone in zones) {
      if (zone.$1 > zone.$2) continue;
      canvas.drawRect(
        Rect.fromLTRB(plot.left, y(zone.$2), plot.right, y(zone.$1)),
        Paint()..color = zone.$3.withValues(alpha: .06),
      );
    }
    for (final level in [-40.0, -30.0, 0.0, 30.0, 40.0]) {
      if (level < low || level > high) continue;
      canvas.drawLine(
        Offset(plot.left, y(level)),
        Offset(plot.right, y(level)),
        Paint()
          ..color = grid
          ..strokeWidth = level == 0 ? 1.2 : .6,
      );
      final painter = TextPainter(
        text: TextSpan(
          text: level.toStringAsFixed(0),
          style: TextStyle(
            color: label,
            fontSize: 8,
            fontWeight: FontWeight.w700,
          ),
        ),
        textDirection: TextDirection.ltr,
      )..layout();
      painter.paint(canvas, Offset(0, y(level) - painter.height / 2));
    }
    final path = Path()..moveTo(x(0), y(values.first));
    for (var index = 1; index < values.length; index++) {
      path.lineTo(x(index), y(values[index]));
    }
    canvas.drawPath(
      path,
      Paint()
        ..shader = const LinearGradient(
          colors: [kZonePut, kZoneMove, kZoneCall],
        ).createShader(plot)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2.6
        ..strokeJoin = StrokeJoin.round,
    );
    canvas.drawCircle(
      Offset(x(values.length - 1), y(values.last)),
      4,
      Paint()..color = zoneColour(_zoneForScore(values.last)),
    );
    if (markerCount > 0) {
      final text = TextPainter(
        text: TextSpan(
          text: '$markerCount trades marked',
          style: TextStyle(
            color: label,
            fontSize: 8.5,
            fontWeight: FontWeight.w700,
          ),
        ),
        textDirection: TextDirection.ltr,
      )..layout();
      text.paint(canvas, Offset(plot.right - text.width, plot.bottom + 5));
    }
  }

  @override
  bool shouldRepaint(covariant _ScoreChartPainter oldDelegate) =>
      oldDelegate.values != values || oldDelegate.markerCount != markerCount;
}

class _ComponentsCard extends StatelessWidget {
  const _ComponentsCard({required this.components});
  final List<Map<String, dynamic>> components;

  @override
  Widget build(BuildContext context) => AppCard(
    kicker: 'Weighted evidence',
    title: 'Components',
    child: components.isEmpty
        ? const Text('No components.', style: AppText.body)
        : MetricWrap(
            children: [
              for (final item in components)
                MetricTile(
                  label: _clean('${item['name'] ?? 'Component'}'),
                  value:
                      item['available'] == false ||
                          _number(item['score']) == null
                      ? 'N/A'
                      : _signed(_number(item['score'])!, 1),
                  colour: signedColour(_number(item['score'])),
                  sub:
                      '${((_number(item['weight']) ?? 0) * 100).round()}% weight',
                ),
            ],
          ),
  );
}

class _TimeframesCard extends StatelessWidget {
  const _TimeframesCard({required this.timeframes});
  final List<Map<String, dynamic>> timeframes;

  @override
  Widget build(BuildContext context) => AppCard(
    kicker: 'Closed candles',
    title: 'Multi-timeframe',
    child: MetricWrap(
      children: [
        for (final item in timeframes)
          MetricTile(
            label: '${item['timeframe'] ?? '—'}'.toUpperCase(),
            value: _number(item['score']) == null
                ? '—'
                : _signed(_number(item['score'])!, 1),
            colour: signedColour(_number(item['score'])),
            sub: _number(item['bias']) == null
                ? null
                : _number(item['bias'])! > 0
                ? 'UP'
                : _number(item['bias'])! < 0
                ? 'DOWN'
                : 'FLAT',
          ),
      ],
    ),
  );
}

class _GatesCard extends StatelessWidget {
  const _GatesCard({required this.gates});
  final List<Map<String, dynamic>> gates;

  @override
  Widget build(BuildContext context) {
    final required = gates.where((gate) => gate['required'] != false).toList();
    final passed = required.where((gate) => gate['passed'] == true).length;
    return AppCard(
      kicker: 'Entry gates',
      title: '$passed/${required.length} passed',
      accent: passed == required.length ? kPositive : kNegative,
      child: Wrap(
        spacing: Gap.sm,
        runSpacing: Gap.sm,
        children: [
          for (final gate in gates)
            StatusPill(
              _clean('${gate['label'] ?? gate['name'] ?? 'Gate'}'),
              colour: gate['status'] == 'DEFERRED'
                  ? kNeutral
                  : gate['passed'] == true
                  ? kPositive
                  : kNegative,
            ),
        ],
      ),
    );
  }
}

class _ReasonCard extends StatelessWidget {
  const _ReasonCard({required this.reasons});
  final List<String> reasons;

  @override
  Widget build(BuildContext context) => AppCard(
    title: 'Why this reading',
    child: Wrap(
      spacing: Gap.xs,
      runSpacing: Gap.xs,
      children: [
        for (final reason in reasons) StatusPill(_clean(reason), dot: false),
      ],
    ),
  );
}

class _EngineStatusCard extends StatelessWidget {
  const _EngineStatusCard({required this.status, required this.snapshot});
  final Map<String, dynamic>? status;
  final Map<String, dynamic> snapshot;

  @override
  Widget build(BuildContext context) {
    final available = status?['available'] == true;
    final uptime = _number(status?['uptime_seconds']);
    return AppCard(
      title: 'Engine status',
      trailing: StatusPill(
        available ? 'REACHABLE' : 'OFFLINE',
        colour: available ? kPositive : kNegative,
      ),
      child: Column(
        children: [
          StatRow(
            'Version',
            '${status?['version'] ?? snapshot['model_version'] ?? '—'}',
          ),
          StatRow(
            'Uptime',
            uptime == null ? '—' : '${(uptime / 3600).toStringAsFixed(1)} h',
          ),
          StatRow('Snapshots', '${status?['snapshots_produced'] ?? '—'}'),
          StatRow('Signal', '${snapshot['signal_id'] ?? '—'}'),
        ],
      ),
    );
  }
}

List<Map<String, dynamic>> _mapList(Object? value) =>
    (value as List<dynamic>? ?? const [])
        .whereType<Map<String, dynamic>>()
        .toList();

double? _number(Object? value) =>
    value is num ? value.toDouble() : double.tryParse('$value');

String _signed(double value, int digits) =>
    '${value > 0 ? '+' : ''}${value.toStringAsFixed(digits)}';

String _clean(String value) => value
    .replaceAll('_', ' ')
    .split(' ')
    .where((part) => part.isNotEmpty)
    .map((part) => '${part[0].toUpperCase()}${part.substring(1).toLowerCase()}')
    .join(' ');

String _regimeLabel(String regime) => switch (regime) {
  'TREND_UP' => 'Bullish trend',
  'TREND_DOWN' => 'Bearish trend',
  'RANGE' => 'Sideways market',
  'BREAKOUT_UP' => 'Bullish breakout',
  'BREAKOUT_DOWN' => 'Bearish breakout',
  'HIGH_VOL_SHOCK' => 'High volatility',
  'LOW_LIQUIDITY' => 'Thin market',
  _ => 'Waiting for reliable data',
};

String _zoneForScore(double? score) {
  if (score == null) return 'HOLD';
  if (score >= 40) return 'CE_2_ITM';
  if (score <= -40) return 'PE_2_ITM';
  if (score.abs() <= 30) return 'SHORT_MOVE';
  return 'HOLD';
}

String _zoneLabel(String zone) => switch (zone) {
  'CE_2_ITM' => 'BUY CE',
  'PE_2_ITM' || 'PE_3_ITM' => 'BUY PE',
  'SHORT_MOVE' => 'SHORT MOVE',
  _ => 'HOLD',
};
