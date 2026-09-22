library;

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../theme/design.dart';
import 'kit.dart';

class TradeActionBar extends StatelessWidget {
  const TradeActionBar({
    super.key,
    required this.onClose,
    required this.onProtection,
    required this.onPayoff,
    this.busy = false,
  });

  final VoidCallback onClose;
  final VoidCallback onProtection;
  final VoidCallback onPayoff;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: Gap.sm,
      runSpacing: Gap.sm,
      children: [
        CompactAction(
          label: busy ? 'Closing…' : 'Close',
          icon: Icons.close_rounded,
          tone: kNegative,
          onPressed: busy ? null : onClose,
        ),
        CompactAction(
          label: 'Protection',
          icon: Icons.shield_outlined,
          onPressed: onProtection,
        ),
        CompactAction(
          label: 'Payoff',
          icon: Icons.area_chart_rounded,
          tone: kWarning,
          onPressed: onPayoff,
        ),
      ],
    );
  }
}

Future<bool> showProtectionEditor({
  required BuildContext context,
  required DashboardApi api,
  required Map<String, dynamic> trade,
}) async {
  final values = <String, TextEditingController>{
    'TP_TARGET_PNL_TREND': _controller(trade['target_pnl']),
    'SL_TARGET_PNL_TREND': _controller(trade['sl_pnl'] ?? 0),
    'TSL_ARM_PNL_TREND': _controller(
      trade['tsl_arm_pnl'] ?? trade['tsl_pnl'] ?? 0,
    ),
    'TSL_TRAIL_PNL_TREND': _controller(
      trade['tsl_trail_pnl'] ?? trade['tsl_pnl'] ?? 0,
    ),
    'TSL_LOCK_MIN_PNL_TREND': _controller(trade['tsl_lock_min_pnl'] ?? 0),
    'TP_POLL_SECS_TREND': _controller(trade['poll_secs'] ?? 10),
  };
  var saving = false;
  String? error;
  final result = await showModalBottomSheet<bool>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    backgroundColor: Theme.of(context).colorScheme.surface,
    builder: (sheetContext) => StatefulBuilder(
      builder: (context, setSheetState) {
        Future<void> save() async {
          final parsed = <String, double>{};
          for (final entry in values.entries) {
            final value = double.tryParse(entry.value.text.trim());
            if (value == null || value < 0) {
              setSheetState(() => error = 'Enter valid non-negative values.');
              return;
            }
            parsed[entry.key] = value;
          }
          if ((parsed['TP_TARGET_PNL_TREND'] ?? 0) < 1 ||
              (parsed['TP_POLL_SECS_TREND'] ?? 0) < 10) {
            setSheetState(
              () => error = r'TP must be at least $1 and polling at least 10s.',
            );
            return;
          }
          setSheetState(() {
            saving = true;
            error = null;
          });
          final response = await api.saveConfig(parsed);
          if (!sheetContext.mounted) return;
          if (response.ok) {
            Navigator.of(sheetContext).pop(true);
          } else {
            setSheetState(() {
              saving = false;
              error = response.error;
            });
          }
        }

        return Padding(
          padding: EdgeInsets.fromLTRB(
            Gap.lg,
            Gap.lg,
            Gap.lg,
            MediaQuery.viewInsetsOf(context).bottom + Gap.lg,
          ),
          child: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              mainAxisSize: MainAxisSize.min,
              children: [
                Row(
                  children: [
                    Icon(
                      Icons.shield_rounded,
                      color: Theme.of(context).colorScheme.primary,
                    ),
                    const SizedBox(width: Gap.sm),
                    const Expanded(
                      child: Text('Position protection', style: AppText.title),
                    ),
                    IconButton(
                      onPressed: () => Navigator.pop(context, false),
                      icon: const Icon(Icons.close_rounded),
                    ),
                  ],
                ),
                const SizedBox(height: Gap.md),
                _ProtectionField(
                  r'Take profit $',
                  values['TP_TARGET_PNL_TREND']!,
                ),
                _ProtectionField(
                  r'Stop loss $',
                  values['SL_TARGET_PNL_TREND']!,
                ),
                _ProtectionField(r'TSL arm $', values['TSL_ARM_PNL_TREND']!),
                _ProtectionField(
                  r'TSL trail $',
                  values['TSL_TRAIL_PNL_TREND']!,
                ),
                _ProtectionField(
                  r'Minimum lock $',
                  values['TSL_LOCK_MIN_PNL_TREND']!,
                ),
                _ProtectionField(
                  'Polling seconds',
                  values['TP_POLL_SECS_TREND']!,
                ),
                if (error != null) ...[
                  const SizedBox(height: Gap.sm),
                  Text(
                    error!,
                    style: AppText.caption.copyWith(color: kNegative),
                  ),
                ],
                const SizedBox(height: Gap.lg),
                FilledButton.icon(
                  onPressed: saving ? null : save,
                  icon: saving
                      ? const SizedBox.square(
                          dimension: 17,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.check_rounded),
                  label: Text(saving ? 'Saving…' : 'Apply protection'),
                ),
              ],
            ),
          ),
        );
      },
    ),
  );
  for (final controller in values.values) {
    controller.dispose();
  }
  return result == true;
}

void showPayoffSheet(BuildContext context, Map<String, dynamic> trade) {
  final symbol = '${trade['symbol'] ?? 'Trade'}'.toUpperCase();
  final strike = _asDouble(trade['strike']) ?? _strikeFrom(symbol);
  final premium = _asDouble(trade['entry_mark']) ?? 0;
  final shortMove = symbol.startsWith('MV-');
  final call = symbol.startsWith('C-');
  final put = symbol.startsWith('P-');

  showModalBottomSheet<void>(
    context: context,
    useSafeArea: true,
    isScrollControlled: true,
    backgroundColor: Theme.of(context).colorScheme.surface,
    builder: (context) => Padding(
      padding: const EdgeInsets.all(Gap.lg),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              const Icon(Icons.area_chart_rounded, color: kWarning),
              const SizedBox(width: Gap.sm),
              Expanded(child: Text(symbol, style: AppText.title)),
              IconButton(
                onPressed: () => Navigator.pop(context),
                icon: const Icon(Icons.close_rounded),
              ),
            ],
          ),
          const SizedBox(height: Gap.md),
          SizedBox(
            height: 220,
            child: CustomPaint(
              painter: _PayoffPainter(
                strike: strike,
                premium: premium,
                call: call,
                put: put,
                shortMove: shortMove,
                grid: Theme.of(context).colorScheme.outline,
              ),
            ),
          ),
          const SizedBox(height: Gap.md),
          MetricWrap(
            children: [
              MetricTile(
                label: 'Strike',
                value: strike == null ? '—' : '\$${strike.toStringAsFixed(0)}',
              ),
              MetricTile(
                label: 'Premium',
                value: premium <= 0 ? '—' : '\$${premium.toStringAsFixed(2)}',
              ),
            ],
          ),
        ],
      ),
    ),
  );
}

TextEditingController _controller(Object? value) =>
    TextEditingController(text: value?.toString() ?? '');

double? _asDouble(Object? value) =>
    value is num ? value.toDouble() : double.tryParse(value?.toString() ?? '');

double? _strikeFrom(String symbol) {
  for (final part in symbol.split('-')) {
    final value = double.tryParse(part);
    if (value != null && value > 1000) return value;
  }
  return null;
}

class _ProtectionField extends StatelessWidget {
  const _ProtectionField(this.label, this.controller);

  final String label;
  final TextEditingController controller;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(bottom: Gap.sm),
    child: TextField(
      controller: controller,
      keyboardType: const TextInputType.numberWithOptions(decimal: true),
      decoration: InputDecoration(labelText: label),
    ),
  );
}

class _PayoffPainter extends CustomPainter {
  const _PayoffPainter({
    required this.strike,
    required this.premium,
    required this.call,
    required this.put,
    required this.shortMove,
    required this.grid,
  });

  final double? strike;
  final double premium;
  final bool call;
  final bool put;
  final bool shortMove;
  final Color grid;

  @override
  void paint(Canvas canvas, Size size) {
    final axis = Paint()
      ..color = grid
      ..strokeWidth = 1;
    canvas.drawLine(
      Offset(0, size.height / 2),
      Offset(size.width, size.height / 2),
      axis,
    );
    canvas.drawLine(
      Offset(size.width / 2, 0),
      Offset(size.width / 2, size.height),
      axis,
    );
    final center = strike ?? 1;
    final range = (premium > 0 ? premium * 3 : center * .05)
        .clamp(1, center)
        .toDouble();
    double payoff(double spot) {
      if (call) {
        return (spot - center).clamp(0, double.infinity).toDouble() - premium;
      }
      if (put) {
        return (center - spot).clamp(0, double.infinity).toDouble() - premium;
      }
      if (shortMove) return premium - (spot - center).abs();
      return 0;
    }

    final points = <Offset>[];
    var maxAbs = 1.0;
    for (var i = 0; i <= 80; i++) {
      final spot = center - range + range * 2 * i / 80;
      maxAbs = [maxAbs, payoff(spot).abs()].reduce((a, b) => a > b ? a : b);
    }
    for (var i = 0; i <= 80; i++) {
      final spot = center - range + range * 2 * i / 80;
      final value = payoff(spot);
      points.add(
        Offset(
          i / 80 * size.width,
          size.height / 2 - value / maxAbs * size.height * .42,
        ),
      );
    }
    final path = Path()..moveTo(points.first.dx, points.first.dy);
    for (final point in points.skip(1)) {
      path.lineTo(point.dx, point.dy);
    }
    canvas.drawPath(
      path,
      Paint()
        ..shader = const LinearGradient(
          colors: [kNegative, kWarning, kPositive],
        ).createShader(Offset.zero & size)
        ..strokeWidth = 3
        ..style = PaintingStyle.stroke
        ..strokeJoin = StrokeJoin.round,
    );
  }

  @override
  bool shouldRepaint(covariant _PayoffPainter oldDelegate) =>
      oldDelegate.strike != strike || oldDelegate.premium != premium;
}
