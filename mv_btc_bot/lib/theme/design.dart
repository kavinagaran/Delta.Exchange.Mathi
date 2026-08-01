/// The app's design system: one source for colour, type, spacing and shape.
///
/// Everything visual should come from here rather than from literals scattered
/// through widgets. The dashboard already learned this lesson on the web side —
/// a hard-coded `35.0` in two files drifted apart — and the same argument
/// applies to a hex code in two screens.
///
/// Two palettes, Red and Blue, mirroring `static/css/app.css`. The neon-green
/// brand accent is deliberately shared by both: on the web it lives outside the
/// per-theme blocks so it reads identically in either, and the app matches.
library;

import 'package:flutter/material.dart';

// ── Brand ───────────────────────────────────────────────────────────────

/// Neon green, mirroring `--neon` in static/css/app.css.
const kNeon = Color(0xFF39FF14);
const kNeonTitle = Color(0xFFEAFFE4);
const kNeonSubtle = Color(0xFF6DFF4D);

/// Layered rather than one wide blur: a tight bright core keeps glyphs legible
/// and the wider faint halos do the neon work. A single large shadow smears.
const kNeonTextGlow = <Shadow>[
  Shadow(color: Color(0xF239FF14), blurRadius: 4),
  Shadow(color: Color(0xB339FF14), blurRadius: 11),
  Shadow(color: Color(0x7339FF14), blurRadius: 24),
];
const kNeonIconGlow = <Shadow>[
  Shadow(color: Color(0xD939FF14), blurRadius: 4),
  Shadow(color: Color(0x7339FF14), blurRadius: 10),
];

// ── Semantic colours, identical in both palettes ────────────────────────
//
// Profit/loss must never depend on the chosen theme: a trader reading red as
// "down" in one palette and "brand" in the other is a real hazard, which is
// why the accent colours below are kept away from these three.

const kPositive = Color(0xFF38D99A);
const kNegative = Color(0xFFFF6172);
const kWarning = Color(0xFFFFC267);
const kNeutral = Color(0xFF8FA3BC);

/// Zone colours, matching the web chart legend.
const kZoneCall = Color(0xFF36EAB2);
const kZonePut = Color(0xFFFF5478);
const kZoneMove = Color(0xFFD9E1EA);
const kZoneHold = Color(0xFF8492A6);

/// One palette. Two instances exist: [redPalette] and [bluePalette].
@immutable
class AppPalette {
  const AppPalette({
    required this.name,
    required this.background,
    required this.surface,
    required this.surfaceRaised,
    required this.border,
    required this.text,
    required this.muted,
    required this.accent,
    required this.accentBright,
    required this.backgroundAsset,
  });

  final String name;
  final Color background;
  final Color surface;
  final Color surfaceRaised;
  final Color border;
  final Color text;
  final Color muted;
  final Color accent;
  final Color accentBright;
  final String backgroundAsset;

  Color get accentSoft => accent.withValues(alpha: .16);
}

const redPalette = AppPalette(
  name: 'Red',
  background: Color(0xFF0D0608),
  surface: Color(0xFF180B0E),
  surfaceRaised: Color(0xFF240F13),
  border: Color(0x38FF5B6C),
  text: Color(0xFFFFF5F6),
  muted: Color(0xFFC4A8AD),
  accent: Color(0xFFFF2F4B),
  accentBright: Color(0xFFFF7A8D),
  backgroundAsset: 'assets/crimson-dashboard-bg.png',
);

const bluePalette = AppPalette(
  name: 'Blue',
  background: Color(0xFF030914),
  surface: Color(0xFF071223),
  surfaceRaised: Color(0xFF081D39),
  border: Color(0x3B5BB4FF),
  text: Color(0xFFF3F9FF),
  muted: Color(0xFFA7BDD6),
  accent: Color(0xFF39A7FF),
  accentBright: Color(0xFF8BD0FF),
  backgroundAsset: 'assets/sparkling-blue-dashboard-bg.png',
);

// ── Spacing ─────────────────────────────────────────────────────────────
//
// A 4pt scale. Named rather than numeric so a screen cannot invent 13px.

abstract final class Gap {
  static const xs = 4.0;
  static const sm = 8.0;
  static const md = 12.0;
  static const lg = 16.0;
  static const xl = 24.0;
  static const xxl = 32.0;
}

abstract final class Radii {
  static const sm = 8.0;
  static const md = 12.0;
  static const lg = 16.0;
  static const pill = 999.0;
}

// ── Type ────────────────────────────────────────────────────────────────
//
// Numbers use tabular figures everywhere. A price column that reflows as
// digits change is the fastest way to make a trading screen feel amateur.

abstract final class AppText {
  static const _tabular = <FontFeature>[FontFeature.tabularFigures()];

  /// Big hero number — account value, day P&L.
  static const display = TextStyle(
    fontSize: 32,
    fontWeight: FontWeight.w800,
    letterSpacing: -.8,
    height: 1.05,
    fontFeatures: _tabular,
  );

  /// Card-level metric.
  static const metric = TextStyle(
    fontSize: 20,
    fontWeight: FontWeight.w700,
    letterSpacing: -.3,
    fontFeatures: _tabular,
  );

  static const title = TextStyle(
    fontSize: 15,
    fontWeight: FontWeight.w700,
    letterSpacing: -.1,
  );

  static const body = TextStyle(fontSize: 13.5, fontWeight: FontWeight.w500);

  static const number = TextStyle(
    fontSize: 13.5,
    fontWeight: FontWeight.w600,
    fontFeatures: _tabular,
  );

  /// Small uppercase label above a value — the web's `.te-kicker`.
  static const kicker = TextStyle(
    fontSize: 9,
    fontWeight: FontWeight.w800,
    letterSpacing: 1.1,
  );

  static const caption = TextStyle(fontSize: 11, fontWeight: FontWeight.w600);
}

/// Colour for a signed value, or [kNeutral] when it is exactly zero.
///
/// Zero is deliberately neutral rather than green: a flat day is not a win,
/// and colouring it green overstates the result at a glance.
Color signedColour(num? value) {
  if (value == null || value == 0) return kNeutral;
  return value > 0 ? kPositive : kNegative;
}

/// Colour for a decision zone name as the engine reports it.
Color zoneColour(String? zone) => switch (zone) {
  'CE_2_ITM' => kZoneCall,
  'PE_2_ITM' || 'PE_3_ITM' => kZonePut,
  'SHORT_MOVE' => kZoneMove,
  _ => kZoneHold,
};
