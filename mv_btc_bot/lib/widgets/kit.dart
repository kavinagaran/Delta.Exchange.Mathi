/// Shared presentation widgets.
///
/// Screens compose these rather than styling containers themselves, so a
/// change to card treatment lands everywhere at once.
library;

import 'package:flutter/material.dart';

import '../theme/design.dart';

/// Section card: a titled surface with an optional trailing accessory.
class AppCard extends StatelessWidget {
  const AppCard({
    super.key,
    this.kicker,
    this.title,
    this.trailing,
    required this.child,
    this.padding = const EdgeInsets.all(Gap.lg),
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
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final hasHeader = kicker != null || title != null || trailing != null;

    return Container(
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            Color.alphaBlend(
              (accent ?? scheme.primary).withValues(alpha: .075),
              scheme.surface.withValues(alpha: .96),
            ),
            scheme.surface.withValues(alpha: .86),
          ],
        ),
        borderRadius: BorderRadius.circular(Radii.lg),
        border: Border.all(color: scheme.outline),
        boxShadow: [
          BoxShadow(
            color: (accent ?? scheme.primary).withValues(alpha: .07),
            blurRadius: 24,
            offset: const Offset(0, 10),
          ),
        ],
      ),
      clipBehavior: Clip.antiAlias,
      child: Stack(
        children: [
          Padding(
            padding: EdgeInsets.fromLTRB(
              padding.left + (accent == null ? 0 : 3),
              padding.top,
              padding.right,
              padding.bottom,
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
                            if (kicker != null)
                              Text(
                                kicker!.toUpperCase(),
                                style: AppText.kicker.copyWith(
                                  color: scheme.onSurfaceVariant,
                                ),
                              ),
                            if (title != null)
                              Padding(
                                padding: const EdgeInsets.only(top: 2),
                                child: Text(title!, style: AppText.title),
                              ),
                          ],
                        ),
                      ),
                      ?trailing,
                    ],
                  ),
                  const SizedBox(height: Gap.md),
                ],
                child,
              ],
            ),
          ),
          if (accent != null)
            Positioned(
              left: 0,
              top: 0,
              bottom: 0,
              width: 3,
              child: ColoredBox(color: accent!),
            ),
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
      minimumSize: const WidgetStatePropertyAll(Size(0, 40)),
      padding: const WidgetStatePropertyAll(
        EdgeInsets.symmetric(horizontal: 13, vertical: 9),
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
        TextStyle(fontSize: 11.5, fontWeight: FontWeight.w700),
      ),
    );
    return TextButton.icon(
      onPressed: onPressed,
      style: style,
      icon: Icon(icon, size: 17),
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
                      color: Theme.of(context).colorScheme.outline,
                    ),
                  ),
                  child: Padding(
                    padding: const EdgeInsets.all(Gap.md),
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
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
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
