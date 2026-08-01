/// Exposure — wallet and every open position on the account.
///
/// Native because this is a data-dense read-only view: on a phone the web
/// version is a horizontally scrolling table, and the numbers that matter
/// (live P&L, distance to liquidation) end up off-screen.
///
/// Shows *every* open position on the Delta account, not only what the bot
/// tracks — a manually opened leg is still exposure and hiding it would make
/// the screen a comfortable lie.
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../theme/design.dart';
import '../widgets/kit.dart';

class ExposureScreen extends StatefulWidget {
  const ExposureScreen({
    super.key,
    required this.api,
    required this.onUnauthorised,
  });

  final DashboardApi api;
  final VoidCallback onUnauthorised;

  @override
  State<ExposureScreen> createState() => _ExposureScreenState();
}

class _ExposureScreenState extends State<ExposureScreen> {
  Map<String, dynamic>? _wallet;
  List<Map<String, dynamic>> _positions = const [];
  String? _error;
  bool _loading = true;
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    _refresh();
    _poll = Timer.periodic(
      const Duration(seconds: 30),
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
      widget.api.wallet(),
      widget.api.allPositions(),
    ]);
    if (!mounted) return;
    if (results.any((r) => r.unauthorised)) {
      widget.onUnauthorised();
      return;
    }

    setState(() {
      _loading = false;
      _wallet = results[0].data as Map<String, dynamic>?;
      _positions = ((results[1].data as List<dynamic>?) ?? const [])
          .whereType<Map<String, dynamic>>()
          .toList();
      _error = results[1].ok ? null : results[1].error;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (_loading && _wallet == null && _positions.isEmpty) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    }

    final totalPnl = _positions.fold<double>(
      0,
      (sum, p) => sum + ((p['live_pnl'] as num?)?.toDouble() ?? 0),
    );

    return RefreshIndicator(
      onRefresh: _refresh,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.md, Gap.lg, Gap.xxl),
        children: [
          _WalletCard(wallet: _wallet),
          const SizedBox(height: Gap.md),
          if (_error != null)
            AppCard(
              kicker: 'Positions',
              title: 'Could not load positions',
              accent: kNegative,
              child: Text(_error!, style: AppText.body),
            )
          else if (_positions.isEmpty)
            const AppCard(
              kicker: 'Positions',
              title: 'Flat',
              child: Text(
                'No open positions on the account.',
                style: AppText.body,
              ),
            )
          else ...[
            AppCard(
              kicker: 'Open exposure',
              title: '${_positions.length} position'
                  '${_positions.length == 1 ? '' : 's'}',
              accent: signedColour(totalPnl),
              trailing: Text(
                _money(totalPnl),
                style: AppText.metric.copyWith(color: signedColour(totalPnl)),
              ),
              child: Text(
                'Every open position on the Delta account, including any '
                'opened by hand.',
                style: AppText.caption.copyWith(
                  color: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
              ),
            ),
            const SizedBox(height: Gap.md),
            for (final position in _positions) ...[
              _PositionCard(position: position),
              const SizedBox(height: Gap.md),
            ],
          ],
        ],
      ),
    );
  }
}

class _WalletCard extends StatelessWidget {
  const _WalletCard({required this.wallet});

  final Map<String, dynamic>? wallet;

  @override
  Widget build(BuildContext context) {
    final error = wallet?['error'];
    if (wallet == null || error != null) {
      return AppCard(
        kicker: 'Wallet',
        title: 'Balance unavailable',
        accent: kWarning,
        child: Text(
          error?.toString() ??
              'The account balance could not be read from Delta.',
          style: AppText.body,
        ),
      );
    }

    final usd = (wallet!['usd_balance'] as num?)?.toDouble();
    final available = (wallet!['usd_available'] as num?)?.toDouble();
    final inr = (wallet!['inr_balance'] as num?)?.toDouble();

    return AppCard(
      kicker: 'Wallet',
      title: 'Account balance',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          MetricTile(
            label: 'Balance',
            value: usd == null ? '—' : _money(usd, signed: false),
            sub: inr == null ? null : '₹${inr.toStringAsFixed(0)}',
            big: true,
          ),
          const SizedBox(height: Gap.md),
          StatRow(
            'Available',
            available == null ? 'Not reported' : _money(available, signed: false),
          ),
        ],
      ),
    );
  }
}

class _PositionCard extends StatelessWidget {
  const _PositionCard({required this.position});

  final Map<String, dynamic> position;

  @override
  Widget build(BuildContext context) {
    final symbol = position['symbol']?.toString() ?? '—';
    final side = position['side']?.toString() ?? '';
    final size = (position['size'] as num?)?.toDouble();
    final entry = (position['entry_price'] as num?)?.toDouble();
    final mark = (position['mark_price'] as num?)?.toDouble();
    final pnl = (position['live_pnl'] as num?)?.toDouble();

    // Deliberately treated as possibly-absent. dashboard.py flags these two as
    // an unverified API shape (assumptions A9): a fabricated liquidation price
    // is far more dangerous than an admitted gap, so they say so rather than
    // rendering a zero.
    final margin = (position['margin'] as num?)?.toDouble();
    final liquidation = (position['liquidation_price'] as num?)?.toDouble();

    final short = side == 'SHORT';
    final distance = (liquidation != null && mark != null && mark > 0)
        ? ((liquidation - mark).abs() / mark * 100)
        : null;

    return AppCard(
      accent: signedColour(pnl),
      kicker: side,
      title: symbol,
      trailing: Text(
        pnl == null ? '—' : _money(pnl),
        style: AppText.metric.copyWith(color: signedColour(pnl)),
      ),
      child: Column(
        children: [
          StatRow('Size', size == null ? '—' : size.toStringAsFixed(0)),
          StatRow('Entry', entry == null ? '—' : entry.toStringAsFixed(2)),
          StatRow('Mark', mark == null ? '—' : mark.toStringAsFixed(2)),
          StatRow('Margin', margin == null ? 'Not reported' : _money(margin, signed: false)),
          StatRow(
            'Liquidation',
            liquidation == null
                ? 'Not reported'
                : liquidation.toStringAsFixed(2),
            valueColour: liquidation == null ? null : kWarning,
          ),
          if (distance != null)
            StatRow(
              'Distance to liquidation',
              '${distance.toStringAsFixed(1)}%',
              // Under 10% is close enough that it should catch the eye, and a
              // short straddle is the position most likely to get there.
              valueColour: distance < 10
                  ? kNegative
                  : (short ? kWarning : null),
            ),
        ],
      ),
    );
  }
}

String _money(double value, {bool signed = true}) {
  final sign = signed && value > 0 ? '+' : '';
  return '$sign\$${value.toStringAsFixed(2)}';
}
