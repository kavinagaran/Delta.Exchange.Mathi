/// Shared presentation widgets.
///
/// Screens compose these rather than styling containers themselves, so a
/// change to card treatment lands everywhere at once.
library;

import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../theme/design.dart';

/// Section card: a titled surface with an optional trailing accessory.
class AppCard extends StatefulWidget {
  const AppCard({
    super.key,
    this.kicker,
    this.title,
    this.trailing,
    required this.child,
    this.padding = const EdgeInsets.all(Gap.md),
    this.accent,
  });

  final String? kicker;
  final String? title;
  final Widget? trailing;
  final Widget child;
  final EdgeInsets padding;

  /// Left edge rule. Used sparingly — it is how a card signals state
  /// (a live position, a failed gate) without shouting in colour.
  final Color? accent;

  @override
  State<AppCard> createState() => _AppCardState();
}

class _AppCardState extends State<AppCard> {
  bool _pressed = false;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final hasHeader =
        widget.kicker != null ||
        widget.title != null ||
        widget.trailing != null;
    final tone = widget.accent ?? scheme.primary;

    return Listener(
      onPointerDown: (_) => setState(() => _pressed = true),
      onPointerUp: (_) => setState(() => _pressed = false),
      onPointerCancel: (_) => setState(() => _pressed = false),
      child: AnimatedScale(
        scale: _pressed ? .992 : 1,
        duration: Motion.fast,
        curve: Motion.curve,
        child: Container(
          decoration: BoxDecoration(
            gradient: LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [
                Color.alphaBlend(
                  tone.withValues(alpha: .13),
                  scheme.surface.withValues(alpha: .97),
                ),
                scheme.surface.withValues(alpha: .90),
                Color.alphaBlend(tone.withValues(alpha: .045), scheme.surface),
              ],
            ),
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: scheme.outline.withValues(alpha: .58)),
            boxShadow: [
              BoxShadow(
                color: Colors.black.withValues(alpha: .28),
                blurRadius: 16,
                offset: const Offset(0, 7),
              ),
              BoxShadow(
                color: tone.withValues(alpha: .10),
                blurRadius: 18,
                offset: const Offset(0, 4),
              ),
            ],
          ),
          clipBehavior: Clip.antiAlias,
          child: Stack(
            children: [
              Positioned(
                left: 14,
                right: 14,
                top: 0,
                height: 1,
                child: DecoratedBox(
                  decoration: BoxDecoration(
                    gradient: LinearGradient(
                      colors: [
                        Colors.transparent,
                        tone.withValues(alpha: .72),
                        Colors.transparent,
                      ],
                    ),
                  ),
                ),
              ),
              Padding(
                padding: EdgeInsets.fromLTRB(
                  widget.padding.left + (widget.accent == null ? 0 : 3),
                  widget.padding.top,
                  widget.padding.right,
                  widget.padding.bottom,
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (hasHeader) ...[
                      Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                if (widget.kicker != null)
                                  Text(
                                    widget.kicker!.toUpperCase(),
                                    style: AppText.kicker.copyWith(
                                      color: scheme.onSurfaceVariant,
                                    ),
                                  ),
                                if (widget.title != null)
                                  Padding(
                                    padding: const EdgeInsets.only(top: 2),
                                    child: Text(
                                      widget.title!,
                                      style: AppText.title,
                                    ),
                                  ),
                              ],
                            ),
                          ),
                          ?widget.trailing,
                        ],
                      ),
                      const SizedBox(height: Gap.md),
                    ],
                    widget.child,
                  ],
                ),
              ),
              if (widget.accent != null)
                Positioned(
                  left: 0,
                  top: 9,
                  bottom: 9,
                  width: 2,
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      color: widget.accent,
                      borderRadius: const BorderRadius.horizontal(
                        right: Radius.circular(5),
                      ),
                      boxShadow: [
                        BoxShadow(
                          color: widget.accent!.withValues(alpha: .65),
                          blurRadius: 10,
                        ),
                      ],
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Compact screen introduction shared by all data-heavy pages. It gives a
/// phone page a clear identity without spending vertical space on prose.
class PageIntro extends StatelessWidget {
  const PageIntro({
    super.key,
    required this.icon,
    required this.title,
    required this.subtitle,
    this.trailing,
  });

  final IconData icon;
  final String title;
  final String subtitle;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.all(Gap.md),
      decoration: BoxDecoration(
        gradient: LinearGradient(
          colors: [
            scheme.primary.withValues(alpha: .24),
            scheme.surface.withValues(alpha: .92),
          ],
        ),
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: scheme.primary.withValues(alpha: .28)),
      ),
      child: Row(
        children: [
          Container(
            width: 36,
            height: 36,
            decoration: BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [scheme.primary, scheme.secondary],
              ),
              borderRadius: BorderRadius.circular(Radii.md),
              boxShadow: [
                BoxShadow(
                  color: scheme.primary.withValues(alpha: .35),
                  blurRadius: 16,
                ),
              ],
            ),
            child: Icon(icon, color: Colors.white, size: 19),
          ),
          const SizedBox(width: Gap.md),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: AppText.title.copyWith(fontSize: 14)),
                const SizedBox(height: 3),
                Text(
                  subtitle,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: AppText.caption.copyWith(
                    color: scheme.onSurfaceVariant,
                  ),
                ),
              ],
            ),
          ),
          ?trailing,
        ],
      ),
    );
  }
}

/// Compact, high-contrast action used inside trade and account cards.
class CompactAction extends StatelessWidget {
  const CompactAction({
    super.key,
    required this.label,
    required this.icon,
    this.onPressed,
    this.tone,
    this.filled = false,
  });

  final String label;
  final IconData icon;
  final VoidCallback? onPressed;
  final Color? tone;
  final bool filled;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final colour = tone ?? scheme.primary;
    final style = ButtonStyle(
      minimumSize: const WidgetStatePropertyAll(Size(0, 34)),
      padding: const WidgetStatePropertyAll(
        EdgeInsets.symmetric(horizontal: 11, vertical: 7),
      ),
      foregroundColor: WidgetStatePropertyAll(filled ? Colors.white : colour),
      backgroundColor: WidgetStatePropertyAll(
        filled ? colour : colour.withValues(alpha: .10),
      ),
      side: WidgetStatePropertyAll(
        BorderSide(color: colour.withValues(alpha: .45)),
      ),
      shape: const WidgetStatePropertyAll(StadiumBorder()),
      textStyle: const WidgetStatePropertyAll(
        TextStyle(fontSize: 10, fontWeight: FontWeight.w700),
      ),
    );
    return TextButton.icon(
      onPressed: onPressed,
      style: style,
      icon: Icon(icon, size: 15),
      label: Text(label),
    );
  }
}

/// Wraps small metrics into equal-width tiles without horizontal scrolling.
class MetricWrap extends StatelessWidget {
  const MetricWrap({super.key, required this.children});

  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) {
        final columns = constraints.maxWidth >= 680 ? 4 : 2;
        final width = (constraints.maxWidth - Gap.sm * (columns - 1)) / columns;
        return Wrap(
          spacing: Gap.sm,
          runSpacing: Gap.sm,
          children: [
            for (final child in children)
              SizedBox(
                width: width,
                child: DecoratedBox(
                  decoration: BoxDecoration(
                    color: Theme.of(context).colorScheme.surfaceContainerHighest
                        .withValues(alpha: .45),
                    borderRadius: BorderRadius.circular(Radii.md),
                    border: Border.all(
                      color: Theme.of(
                        context,
                      ).colorScheme.outline.withValues(alpha: .55),
                    ),
                  ),
                  child: Padding(
                    padding: const EdgeInsets.all(Gap.sm),
                    child: child,
                  ),
                ),
              ),
          ],
        );
      },
    );
  }
}

/// One label/value row. The value is right-aligned and tabular so a column of
/// these lines up digit for digit.
class StatRow extends StatelessWidget {
  const StatRow(
    this.label,
    this.value, {
    super.key,
    this.valueColour,
    this.mono = true,
  });

  final String label;
  final String value;
  final Color? valueColour;
  final bool mono;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 5),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Text(
              label,
              style: AppText.body.copyWith(color: scheme.onSurfaceVariant),
            ),
          ),
          const SizedBox(width: Gap.md),
          Flexible(
            child: Text(
              value,
              textAlign: TextAlign.right,
              style: (mono ? AppText.number : AppText.body).copyWith(
                color: valueColour ?? scheme.onSurface,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Hero metric — one big number with a caption.
class MetricTile extends StatelessWidget {
  const MetricTile({
    super.key,
    required this.label,
    required this.value,
    this.sub,
    this.colour,
    this.big = false,
  });

  final String label;
  final String value;
  final String? sub;
  final Color? colour;
  final bool big;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(
          label.toUpperCase(),
          style: AppText.kicker.copyWith(color: scheme.onSurfaceVariant),
        ),
        const SizedBox(height: Gap.xs),
        Text(
          value,
          style: (big ? AppText.display : AppText.metric).copyWith(
            color: colour ?? scheme.onSurface,
          ),
        ),
        if (sub != null) ...[
          const SizedBox(height: 2),
          Text(
            sub!,
            style: AppText.caption.copyWith(color: scheme.onSurfaceVariant),
          ),
        ],
      ],
    );
  }
}

/// Status pill with a leading dot — the web's `.pill`.
class StatusPill extends StatelessWidget {
  const StatusPill(this.label, {super.key, this.colour, this.dot = true});

  final String label;
  final Color? colour;
  final bool dot;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final tone = colour ?? scheme.onSurfaceVariant;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: tone.withValues(alpha: .13),
        borderRadius: BorderRadius.circular(Radii.pill),
        border: Border.all(color: tone.withValues(alpha: .38)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (dot) ...[
            Container(
              width: 6,
              height: 6,
              decoration: BoxDecoration(color: tone, shape: BoxShape.circle),
            ),
            const SizedBox(width: 6),
          ],
          Text(
            label,
            style: AppText.caption.copyWith(
              color: tone,
              fontWeight: FontWeight.w700,
            ),
          ),
        ],
      ),
    );
  }
}

/// Compact view of the committed 5-minute ADX policy shared by Today and the
/// full Trend Engine screen. Execution remains server-owned; this widget only
/// makes the exact evidence and boundary visible on the phone.
class CommittedAdxPill extends StatelessWidget {
  const CommittedAdxPill({super.key, required this.adx, required this.zone});

  final double? adx;
  final String? zone;

  @override
  Widget build(BuildContext context) {
    final value = adx;
    if (value == null || !value.isFinite) {
      return const StatusPill('5M ADX —', colour: kNeutral, dot: false);
    }
    final calm = value <= 20;
    final invalidatesMove = !calm && zone == 'SHORT_MOVE';
    final label = invalidatesMove
        ? '5M ADX ${value.toStringAsFixed(1)} · EXIT MOVE'
        : calm
        ? '5M ADX ${value.toStringAsFixed(1)} · CALM'
        : '5M ADX ${value.toStringAsFixed(1)} · TREND';
    final tone = invalidatesMove
        ? kNegative
        : calm
        ? kZoneMove
        : kPositive;
    return StatusPill(label, colour: tone, dot: false);
  }
}

/// A circular score gauge shared by Today and Trend Engine. The ring and the
/// large centre value continuously interpolate from red to green by score.
class DecisionScoreDial extends StatelessWidget {
  const DecisionScoreDial({
    super.key,
    required this.label,
    required this.score,
    required this.colour,
    this.maxSize = 126,
  });

  final String label;
  final double? score;
  final Color colour;
  final double maxSize;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Semantics(
      label: '$label score',
      value: score?.toStringAsFixed(1) ?? 'unavailable',
      child: LayoutBuilder(
        builder: (context, constraints) {
          final width = constraints.maxWidth.isFinite
              ? constraints.maxWidth
              : maxSize;
          final height = constraints.maxHeight.isFinite
              ? constraints.maxHeight
              : maxSize;
          final dimension = math.min(maxSize, math.min(width, height));
          return Center(
            child: SizedBox.square(
              dimension: dimension,
              child: CustomPaint(
                painter: _DecisionGaugePainter(score: score, tone: colour),
                child: Center(
                  child: Padding(
                    padding: const EdgeInsets.all(22),
                    child: FittedBox(
                      fit: BoxFit.scaleDown,
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Text(
                            label.toUpperCase(),
                            style: AppText.kicker.copyWith(
                              color: scheme.onSurface,
                              fontSize: 7,
                              fontWeight: FontWeight.w800,
                              letterSpacing: .6,
                            ),
                          ),
                          const SizedBox(height: 3),
                          _DialScore(score: score, colour: scoreColour(score)),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ),
          );
        },
      ),
    );
  }
}

class _DialScore extends StatelessWidget {
  const _DialScore({required this.score, required this.colour});

  final double? score;
  final Color colour;

  @override
  Widget build(BuildContext context) {
    if (score == null || !score!.isFinite) {
      return Text('—', style: _style);
    }
    final bounded = score!.clamp(-100.0, 100.0).toDouble();
    final value = '${bounded > 0 ? '+' : ''}${bounded.toStringAsFixed(1)}';
    return Text(value, style: _style);
  }

  TextStyle get _style => AppText.metric.copyWith(
    color: colour,
    fontSize: 29,
    fontWeight: FontWeight.w900,
    fontFeatures: const [FontFeature.tabularFigures()],
    shadows: [Shadow(color: colour.withValues(alpha: .44), blurRadius: 8)],
  );
}

class _DecisionGaugePainter extends CustomPainter {
  const _DecisionGaugePainter({required this.score, required this.tone});

  final double? score;
  final Color tone;

  static const _start = -math.pi / 2;
  static const _sweep = math.pi * 2;

  @override
  void paint(Canvas canvas, Size size) {
    final centre = Offset(size.width / 2, size.height / 2);
    final radius = math.min(size.width, size.height) / 2 - 4;
    canvas.drawCircle(
      centre,
      radius,
      Paint()
        ..style = PaintingStyle.fill
        ..color = const Color(0xFF081A2E),
    );
    final bounded = score?.clamp(-100.0, 100.0).toDouble();
    final scoreTone = bounded == null ? tone : scoreColour(bounded);
    canvas.drawCircle(
      centre,
      radius,
      Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2
        ..color = scoreTone.withValues(alpha: .55)
        ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 4),
    );
    canvas.drawCircle(
      centre,
      radius - 1,
      Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.1
        ..color = scoreTone.withValues(alpha: .58),
    );

    final arcRect = Rect.fromCircle(center: centre, radius: radius - 9);
    canvas.drawArc(
      arcRect,
      _start,
      _sweep,
      false,
      Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 10
        ..strokeCap = StrokeCap.round
        ..color = const Color(0xFF17314A),
    );
    if (bounded != null) {
      final progress = scoreFillFraction(bounded) * _sweep;
      canvas.drawArc(
        arcRect,
        _start,
        progress,
        false,
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeWidth = 12
          ..strokeCap = StrokeCap.round
          ..color = scoreTone.withValues(alpha: .38)
          ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 5),
      );
      canvas.drawArc(
        arcRect,
        _start,
        progress,
        false,
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeWidth = 9
          ..strokeCap = StrokeCap.round
          ..color = scoreTone,
      );
    }

    canvas.drawCircle(
      centre,
      radius - 20,
      Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1
        ..color = scoreTone.withValues(alpha: .32),
    );
  }

  @override
  bool shouldRepaint(covariant _DecisionGaugePainter oldDelegate) =>
      oldDelegate.score != score || oldDelegate.tone != tone;
}

String tradeDecisionLabel(
  String? zone, {
  required bool actionAllowed,
  String? reason,
}) {
  final key = (zone ?? '').toUpperCase();
  final blocker = reason ?? '';
  if (key == 'HOLD') return 'HOLD — NO NEW TRADE';
  if (key.isEmpty) return 'NO DECISION';
  if (!actionAllowed) {
    if (key == 'SHORT_MOVE' && blocker.toUpperCase().contains('ADX')) {
      return 'WAIT — 5M ADX NOT CALM';
    }
    if (key == 'SHORT_MOVE' && blocker.toLowerCase().contains('stop loss')) {
      return 'WAIT — STOP REQUIRED';
    }
    return 'WAIT — CHECKS BLOCKED';
  }
  return switch (key) {
    'CE_2_ITM' => 'BUY 2-STEP ITM CE',
    'PE_2_ITM' || 'PE_3_ITM' => 'BUY 2-STEP ITM PE',
    'SHORT_MOVE' => 'SELL ATM MOVE',
    _ => key.replaceAll('_', ' '),
  };
}

class ScoreDecisionPill extends StatelessWidget {
  const ScoreDecisionPill({
    super.key,
    required this.label,
    required this.score,
  });

  final String label;
  final double? score;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final tone = scoreColour(score);
    return Align(
      alignment: Alignment.center,
      child: Container(
        constraints: const BoxConstraints(minWidth: 220),
        padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 8),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(999),
          border: Border.all(color: tone.withValues(alpha: .64)),
          gradient: LinearGradient(
            colors: [
              Color.lerp(scheme.surface, tone, .24)!,
              Color.lerp(scheme.surfaceContainer, tone, .08)!,
            ],
          ),
          boxShadow: [
            BoxShadow(color: tone.withValues(alpha: .16), blurRadius: 16),
          ],
        ),
        child: Text(
          label,
          textAlign: TextAlign.center,
          style: AppText.kicker.copyWith(
            color: tone,
            fontSize: 9,
            fontWeight: FontWeight.w900,
            letterSpacing: .35,
          ),
        ),
      ),
    );
  }
}

/// Empty / error / loading placeholder, so every screen fails the same way.
class StatePlaceholder extends StatelessWidget {
  const StatePlaceholder({
    super.key,
    required this.icon,
    required this.message,
    this.detail,
    this.onRetry,
    this.tone,
  });

  final IconData icon;
  final String message;
  final String? detail;
  final VoidCallback? onRetry;
  final Color? tone;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final colour = tone ?? scheme.onSurfaceVariant;
    return Padding(
      padding: const EdgeInsets.symmetric(
        vertical: Gap.xxl,
        horizontal: Gap.lg,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 34, color: colour.withValues(alpha: .75)),
          const SizedBox(height: Gap.md),
          Text(
            message,
            textAlign: TextAlign.center,
            style: AppText.body.copyWith(color: scheme.onSurface),
          ),
          if (detail != null) ...[
            const SizedBox(height: Gap.xs),
            Text(
              detail!,
              textAlign: TextAlign.center,
              style: AppText.caption.copyWith(color: scheme.onSurfaceVariant),
            ),
          ],
          if (onRetry != null) ...[
            const SizedBox(height: Gap.lg),
            OutlinedButton.icon(
              onPressed: onRetry,
              icon: const Icon(Icons.refresh_rounded, size: 18),
              label: const Text('Retry'),
            ),
          ],
        ],
      ),
    );
  }
}

/// Horizontal score meter for the −100…+100 decision score, with the live
/// zone boundaries marked. Reads at a glance where the score sits relative to
/// the thresholds that actually trigger a trade.
class ScoreMeter extends StatelessWidget {
  const ScoreMeter({
    super.key,
    required this.score,
    this.entryAbs = 40,
    this.sidewaysAbs = 30,
  });

  final double score;
  final double entryAbs;
  final double sidewaysAbs;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final clamped = score.clamp(-100.0, 100.0);
    return LayoutBuilder(
      builder: (context, constraints) {
        final width = constraints.maxWidth;
        double at(double value) => (value + 100) / 200 * width;
        return SizedBox(
          height: 34,
          child: Stack(
            clipBehavior: Clip.none,
            children: [
              // Track, with the sideways band picked out.
              Positioned(
                left: 0,
                right: 0,
                top: 13,
                child: Container(
                  height: 6,
                  decoration: BoxDecoration(
                    color: scheme.surfaceContainerHighest,
                    borderRadius: BorderRadius.circular(Radii.pill),
                  ),
                ),
              ),
              Positioned(
                left: at(-sidewaysAbs),
                width: at(sidewaysAbs) - at(-sidewaysAbs),
                top: 13,
                child: Container(
                  height: 6,
                  color: kZoneMove.withValues(alpha: .22),
                ),
              ),
              for (final edge in [-entryAbs, entryAbs])
                Positioned(
                  left: at(edge) - .5,
                  top: 9,
                  child: Container(
                    width: 1,
                    height: 14,
                    color: (edge > 0 ? kZoneCall : kZonePut).withValues(
                      alpha: .85,
                    ),
                  ),
                ),
              // Marker.
              Positioned(
                left: (at(clamped) - 7).clamp(0.0, width - 14),
                top: 6,
                child: Container(
                  width: 14,
                  height: 20,
                  decoration: BoxDecoration(
                    color: signedColour(clamped),
                    borderRadius: BorderRadius.circular(4),
                    boxShadow: [
                      BoxShadow(
                        color: signedColour(clamped).withValues(alpha: .5),
                        blurRadius: 10,
                      ),
                    ],
                  ),
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}
