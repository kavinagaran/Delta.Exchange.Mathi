/// Cockpit — setup-gated manual LIVE option execution.
///
/// This native screen mirrors the web Cockpit contract: the server decides
/// which market setups are eligible, a green setup unlocks only its matching
/// BUY/SELL strategies, every order is previewed, and the server revalidates
/// setup eligibility again before the LIVE submission.
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../theme/design.dart';
import '../widgets/kit.dart';

class CockpitScreen extends StatefulWidget {
  const CockpitScreen({
    super.key,
    required this.api,
    required this.onUnauthorised,
  });

  final DashboardApi api;
  final VoidCallback onUnauthorised;

  @override
  State<CockpitScreen> createState() => _CockpitScreenState();
}

class _SetupSpec {
  const _SetupSpec(this.id, this.label, this.fallbackDetail);

  final String id;
  final String label;
  final String fallbackDetail;
}

class _SetupGroup {
  const _SetupGroup(this.label, this.setups);

  final String label;
  final List<_SetupSpec> setups;
}

class _StrategySpec {
  const _StrategySpec({
    required this.action,
    required this.title,
    required this.detail,
    required this.badge,
    required this.buy,
  });

  final String action;
  final String title;
  final String detail;
  final String badge;
  final bool buy;
}

const _setupGroups = <_SetupGroup>[
  _SetupGroup('Trend & momentum', [
    _SetupSpec('trend_bullish', 'Bullish trend score', 'Score > +40'),
    _SetupSpec('trend_bearish', 'Bearish trend score', 'Score < −40'),
    _SetupSpec(
      'ema_bullish',
      'EMA bullish alignment',
      'Higher + lower timeframe',
    ),
    _SetupSpec(
      'ema_bearish',
      'EMA bearish alignment',
      'Higher + lower timeframe',
    ),
    _SetupSpec('rsi_bullish', 'RSI bullish momentum', 'RSI component'),
    _SetupSpec('rsi_bearish', 'RSI bearish momentum', 'RSI component'),
    // Ungated operator override. The server reports it eligible
    // unconditionally and returns every action, so the strategy panel
    // unlocks all six from the same `actions` payload as any other setup.
    _SetupSpec(
      'lemme_risk',
      'Lemme Risk',
      'Ungated · every strategy · TP/SL/TSL',
    ),
  ]),
  _SetupGroup('Structure & flow', [
    _SetupSpec(
      'supertrend_bullish',
      'Supertrend bullish flip',
      'Confirmed engine reason',
    ),
    _SetupSpec(
      'supertrend_bearish',
      'Supertrend bearish flip',
      'Confirmed engine reason',
    ),
    _SetupSpec('support_bounce', 'Support bounce', 'Positive market structure'),
    _SetupSpec(
      'resistance_rejection',
      'Resistance rejection',
      'Negative market structure',
    ),
    _SetupSpec('breakout_bullish', 'Bullish breakout', 'Breakout quality'),
    _SetupSpec('breakout_bearish', 'Bearish breakdown', 'Breakout quality'),
    _SetupSpec(
      'orderflow_buy',
      'Order-flow buy pressure',
      'Live order-flow component',
    ),
    _SetupSpec(
      'orderflow_sell',
      'Order-flow sell pressure',
      'Live order-flow component',
    ),
  ]),
  _SetupGroup('Volatility', [
    _SetupSpec('calm_range', 'Calm / range market', '|score| ≤ 30 · ADX ≤ 20'),
    _SetupSpec(
      'volatility_expansion',
      'Volatility expansion',
      'ADX > 25 · breakout active',
    ),
  ]),
];

const _strategies = <_StrategySpec>[
  _StrategySpec(
    action: 'buy_ce',
    title: '2-Step ITM Call',
    detail: 'Bullish directional · CE',
    badge: 'CE',
    buy: true,
  ),
  _StrategySpec(
    action: 'buy_pe',
    title: '2-Step ITM Put',
    detail: 'Bearish directional · PE',
    badge: 'PE',
    buy: true,
  ),
  _StrategySpec(
    action: 'buy_move',
    title: 'ATM MOVE',
    detail: 'Long volatility · limited risk',
    badge: 'MV',
    buy: true,
  ),
  _StrategySpec(
    action: 'sell_ce',
    title: 'ATM Call',
    detail: 'Bearish premium sell · protected',
    badge: 'CE',
    buy: false,
  ),
  _StrategySpec(
    action: 'sell_pe',
    title: 'ATM Put',
    detail: 'Bullish premium sell · protected',
    badge: 'PE',
    buy: false,
  ),
  _StrategySpec(
    action: 'sell_move',
    title: 'ATM MOVE Straddle',
    detail: 'Short volatility · TP / SL / TSL protected',
    badge: 'MV',
    buy: false,
  ),
];

class _CockpitScreenState extends State<CockpitScreen> {
  Map<String, dynamic>? _market;
  Map<String, dynamic>? _controller;
  List<Map<String, dynamic>> _todayTrades = const [];
  String? _selectedSetup;
  String? _selectedAction;
  String? _error;
  String? _status;
  bool _statusError = false;
  bool _loading = true;
  bool _busy = false;
  bool _resettingLock = false;
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    _refresh();
    _poll = Timer.periodic(
      const Duration(seconds: 10),
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
      widget.api.cockpitSetups(),
      widget.api.scoreAutoStatus(),
      widget.api.todayTrades(),
    ]);
    if (!mounted) return;
    if (results.any((result) => result.unauthorised)) {
      widget.onUnauthorised();
      return;
    }
    final marketResult = results[0];
    final controllerResult = results[1];
    final tradesResult = results[2];
    final nextMarket = marketResult.data as Map<String, dynamic>?;
    final nextController = controllerResult.data as Map<String, dynamic>?;
    final nextTrades = (tradesResult.data as List<dynamic>? ?? const [])
        .whereType<Map<String, dynamic>>()
        .toList();
    setState(() {
      _loading = false;
      _market = nextMarket;
      _controller = nextController;
      _todayTrades = nextTrades;
      _error = marketResult.ok
          ? null
          : (marketResult.error ?? 'Cockpit setup data is unavailable');
      if (_selectedSetup != null && !_setupEligible(_selectedSetup!)) {
        _selectedSetup = null;
        _selectedAction = null;
      } else if (_selectedAction != null &&
          !_selectedActions.contains(_selectedAction)) {
        _selectedAction = null;
      }
    });
  }

  Map<String, dynamic>? _setupState(String id) {
    final setups = _market?['setups'];
    if (setups is! Map) return null;
    final value = setups[id];
    return value is Map ? Map<String, dynamic>.from(value) : null;
  }

  bool _setupEligible(String id) => _setupState(id)?['eligible'] == true;

  Set<String> get _selectedActions {
    final state = _selectedSetup == null ? null : _setupState(_selectedSetup!);
    final actions = state?['actions'];
    if (actions is! List) return const {};
    return actions.map((value) => '$value').toSet();
  }

  bool get _hasOpenPosition {
    final durable = '${_controller?['position_status'] ?? ''}'.toUpperCase();
    if (durable == 'OPEN' || durable == 'ENTRY_PENDING') return true;
    return _todayTrades.any((trade) {
      final open =
          trade['_live'] == true ||
          '${trade['status'] ?? ''}'.toUpperCase() == 'OPEN';
      final slot = '${trade['control_slot'] ?? trade['slot'] ?? 'trend'}'
          .toLowerCase();
      return open && slot == 'trend';
    });
  }

  String get _accountTradingMode {
    final value = '${_controller?['account_trading_mode'] ?? ''}'.toUpperCase();
    if (value.isNotEmpty) return value;
    return _controller?['account_live'] == true ? 'LIVE' : 'DRY RUN';
  }

  bool get _accountLive =>
      _controller?['account_live'] == true || _accountTradingMode == 'LIVE';
  bool get _accountDryRun => _accountTradingMode == 'DRY RUN';
  bool get _accountModeReady => _accountLive || _accountDryRun;
  bool get _canTrade =>
      _accountModeReady && !_hasOpenPosition && !_busy;

  Map<String, dynamic>? get _lock {
    final value = _controller?['setup_lock'];
    return value is Map ? Map<String, dynamic>.from(value) : null;
  }

  bool get _lockActive => _lock?['active'] == true;

  void _selectSetup(String id) {
    if (!_setupEligible(id) || _busy) return;
    setState(() {
      _selectedSetup = id;
      _selectedAction = null;
      _status = null;
      _statusError = false;
    });
  }

  void _selectAction(String action) {
    if (!_canTrade || !_selectedActions.contains(action)) return;
    setState(() {
      _selectedAction = action;
      _status = null;
      _statusError = false;
    });
  }

  Future<void> _previewSelected() async {
    final setup = _selectedSetup;
    final action = _selectedAction;
    if (setup == null || action == null || !_canTrade) return;
    setState(() {
      _busy = true;
      _statusError = false;
      _status = 'Resolving ${_strategy(action).title}…';
    });
    final preview = await widget.api.cockpitPreview(action, setup);
    if (!mounted) return;
    final data = preview.data;
    if (!preview.ok || data == null) {
      setState(() {
        _busy = false;
        _statusError = true;
        _status = preview.error ?? 'Could not resolve this strategy';
      });
      return;
    }
    setState(() => _status = null);
    final confirmed = await _showOrderPreview(
      setup: setup,
      action: action,
      preview: data,
    );
    if (!mounted) return;
    if (confirmed != true) {
      setState(() => _busy = false);
      return;
    }
    setState(() => _status = 'Placing ${_strategy(action).title}…');
    final result = await widget.api.cockpitEnter(action, setup);
    if (!mounted) return;
    final resultData = result.data;
    final ok = result.ok && resultData != null;
    final state = ok && resultData['state'] is Map
        ? Map<String, dynamic>.from(resultData['state'] as Map)
        : null;
    final dryRun = resultData?['dry_run'] == true;
    final message = ok
        ? '${_strategy(action).title} ${dryRun ? 'simulation opened' : 'filled'} · '
              '${state?['symbol'] ?? ''} · ${state?['lots'] ?? '—'} lots. '
              '${dryRun ? 'View it in DRY RUN.' : ''}'
        : (result.error ?? 'Cockpit order failed');
    setState(() {
      _busy = false;
      _statusError = !ok;
      _status = message;
    });
    _notify(message, ok: ok);
    await _refresh(quiet: true);
  }

  Future<bool?> _showOrderPreview({
    required String setup,
    required String action,
    required Map<String, dynamic> preview,
  }) {
    final strategy = _strategy(action);
    final setupLabel = _setupSpec(setup).label;
    final price = _number(preview['entry_price']);
    final strike = _number(preview['strike']);
    final move = '${preview['instrument_kind']}' == 'BTC_MOVE';
    final dryRun =
        preview['dry_run'] == true || preview['execution_mode'] == 'dry_run';
    return showDialog<bool>(
      context: context,
      useSafeArea: true,
      builder: (context) => Center(
        child: AlertDialog(
          title: Text(dryRun ? 'Confirm DRY RUN trade' : 'Confirm LIVE order'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _PreviewLine('Setup', setupLabel),
                _PreviewLine('Strategy', strategy.title),
                _PreviewLine('Side', '${preview['side'] ?? '—'}'.toUpperCase()),
                _PreviewLine('Contract', '${preview['symbol'] ?? '—'}'),
                _PreviewLine(
                  'Strike',
                  move
                      ? 'ATM MOVE'
                      : strike == null
                      ? '—'
                      : '\$${strike.toStringAsFixed(0)}',
                ),
                _PreviewLine(
                  'Entry price',
                  price == null ? '—' : '\$${price.toStringAsFixed(4)}',
                ),
                _PreviewLine(
                  dryRun ? 'Simulation lots' : 'Affordable lots',
                  '${preview['lots'] ?? '—'}',
                ),
                const _PreviewLine('Protection', 'Automatic TP / SL / TSL'),
                const SizedBox(height: Gap.md),
                Text(
                  dryRun
                      ? 'This opens a DRY RUN simulation. No order is sent '
                            'to Delta Exchange.'
                      : 'This submits a real marketable LIVE order. Verify '
                            'the contract and affordable size before confirming.',
                  style: AppText.caption.copyWith(color: kWarning),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: Text(dryRun ? 'Open DRY RUN Trade' : 'Place LIVE Order'),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _resetZoneLock() async {
    if (!_lockActive || _resettingLock) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Reset the current zone lock?'),
        content: const Text(
          'This permits one fresh entry in the current zone. It does not '
          'close a position or submit an order.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Reset'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() => _resettingLock = true);
    final result = await widget.api.resetZoneLock();
    if (!mounted) return;
    setState(() => _resettingLock = false);
    final data = result.data;
    final released = data is Map && data['released'] == true;
    final message = result.ok
        ? released
              ? 'Zone lock reset. One fresh entry is permitted.'
              : '${data is Map ? data['message'] ?? 'No zone lock is active' : 'No zone lock is active'}'
        : result.error ?? 'Zone-lock reset failed';
    _notify(message, ok: result.ok);
    await _refresh(quiet: true);
  }

  void _notify(String message, {required bool ok}) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(message),
        backgroundColor: ok ? kPositive : kNegative,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_loading && _market == null) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    }
    if (_error != null && _market == null) {
      return StatePlaceholder(
        icon: Icons.sports_esports_rounded,
        message: 'Cockpit is unavailable',
        detail: _error,
        onRetry: _refresh,
        tone: kNegative,
      );
    }
    return RefreshIndicator(
      onRefresh: _refresh,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.md, Gap.lg, Gap.xxl),
        children: [
          _CommandStrip(
            modeLabel: '${_accountDryRun ? 'DRY RUN' : 'LIVE'} COCKPIT',
            message: !_accountModeReady
                ? 'Cockpit locked · account execution mode is unavailable.'
                : _hasOpenPosition
                ? 'Cockpit locked · an active position is already open.'
                : _status ??
                      '${_accountDryRun ? 'DRY RUN' : 'LIVE'} COCKPIT · '
                          'select any green setup for a manual order.',
            error: _statusError,
            ready: _canTrade && !_statusError,
          ),
          const SizedBox(height: Gap.md),
          _readinessPanel(),
          const SizedBox(height: Gap.md),
          _marketSetupPanel(),
          const SizedBox(height: Gap.md),
          _strategyPanel(),
          const SizedBox(height: Gap.md),
          _SafetyStrip(dryRun: _accountDryRun),
        ],
      ),
    );
  }

  Widget _readinessPanel() {
    final lock = _lock;
    final lockZone = '${lock?['zone'] ?? lock?['target_zone'] ?? 'NONE'}'
        .replaceAll('_', ' ');
    return LayoutBuilder(
      builder: (context, constraints) {
        final width = (constraints.maxWidth - Gap.sm) / 2;
        return Wrap(
          spacing: Gap.sm,
          runSpacing: Gap.sm,
          children: [
            SizedBox(
              width: width,
              child: _StateTile(
                label: 'Trading mode',
                value: _accountLive ? 'LIVE READY' : 'DRY RUN READY',
                detail: _accountLive
                    ? 'Orders go to Delta Exchange'
                    : 'Orders go to the DRY RUN dashboard',
                tone: _accountModeReady ? kPositive : kWarning,
              ),
            ),
            SizedBox(
              width: width,
              child: _StateTile(
                label: 'Position',
                value: _hasOpenPosition ? 'OCCUPIED' : 'CLEAR',
                detail: _hasOpenPosition
                    ? 'One active Trend position'
                    : 'Trend slot available',
                tone: _hasOpenPosition ? kWarning : kPositive,
              ),
            ),
            SizedBox(
              width: width,
              child: _StateTile(
                label: 'Zone lock',
                value: _lockActive ? lockZone : 'NONE',
                detail: _lockActive
                    ? 'Repeat entry prevented'
                    : 'No active lock',
                tone: _lockActive ? kWarning : kPositive,
                trailing: CompactAction(
                  label: _resettingLock ? 'Resetting…' : 'Reset',
                  icon: Icons.lock_open_rounded,
                  onPressed: _lockActive && !_resettingLock
                      ? _resetZoneLock
                      : null,
                ),
              ),
            ),
          ],
        );
      },
    );
  }

  Widget _marketSetupPanel() {
    final score = _number(_market?['score']);
    final adx = _number(_market?['adx']);
    return AppCard(
      kicker: '1 · Market setup',
      title: 'Select one eligible signal',
      accent: kPositive,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          MetricWrap(
            children: [
              MetricTile(
                label: 'Committed score',
                value: score == null
                    ? '—'
                    : '${score >= 0 ? '+' : ''}${score.toStringAsFixed(1)}',
                colour: scoreColour(score),
              ),
              MetricTile(
                label: '15M ADX',
                value: adx?.toStringAsFixed(1) ?? '—',
                colour: adx != null && adx <= 20 ? kPositive : kWarning,
              ),
              MetricTile(
                label: 'Regime',
                value: '${_market?['regime'] ?? '—'}'.replaceAll('_', ' '),
              ),
              MetricTile(
                label: 'Data',
                value: '${_market?['data_quality'] ?? '—'}',
                colour: _market?['data_quality'] == 'OK' ? kPositive : kWarning,
              ),
            ],
          ),
          const SizedBox(height: Gap.lg),
          for (final group in _setupGroups) ...[
            Text(group.label.toUpperCase(), style: AppText.kicker),
            const SizedBox(height: Gap.sm),
            LayoutBuilder(
              builder: (context, constraints) {
                final columns = constraints.maxWidth >= 720 ? 2 : 1;
                final width =
                    (constraints.maxWidth - Gap.sm * (columns - 1)) / columns;
                return Wrap(
                  spacing: Gap.sm,
                  runSpacing: Gap.sm,
                  children: [
                    for (final setup in group.setups)
                      SizedBox(
                        width: width,
                        child: _SetupTile(
                          spec: setup,
                          state: _setupState(setup.id),
                          selected: _selectedSetup == setup.id,
                          onTap: () => _selectSetup(setup.id),
                        ),
                      ),
                  ],
                );
              },
            ),
            const SizedBox(height: Gap.lg),
          ],
        ],
      ),
    );
  }

  Widget _strategyPanel() {
    final setup = _selectedSetup == null ? null : _setupSpec(_selectedSetup!);
    return AppCard(
      kicker: '2 · Option strategy',
      title: setup?.label ?? 'Choose a green setup first',
      accent: Theme.of(context).colorScheme.primary,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (setup != null)
            _SelectedSetupBanner(
              title: setup.label,
              detail:
                  '${_setupState(setup.id)?['detail'] ?? setup.fallbackDetail}',
              tone: _setupState(setup.id)?['override'] == true
                  ? kWarning
                  : kPositive,
            ),
          if (setup != null) const SizedBox(height: Gap.md),
          _StrategySection(
            title: 'BUY',
            subtitle: 'Premium-paid strategies',
            tone: kPositive,
            strategies: _strategies.where((item) => item.buy).toList(),
            allowed: _selectedActions,
            selected: _selectedAction,
            canTrade: _canTrade,
            onSelected: _selectAction,
          ),
          const SizedBox(height: Gap.md),
          _StrategySection(
            title: 'SELL',
            subtitle: 'Protected premium-selling strategies',
            tone: kNegative,
            strategies: _strategies.where((item) => !item.buy).toList(),
            allowed: _selectedActions,
            selected: _selectedAction,
            canTrade: _canTrade,
            onSelected: _selectAction,
          ),
          const SizedBox(height: Gap.md),
          Row(
            children: [
              Expanded(
                child: Text(
                  _selectedAction == null
                      ? 'Select an eligible strategy'
                      : _strategy(_selectedAction!).title,
                  style: AppText.title,
                ),
              ),
              const SizedBox(width: Gap.md),
              FilledButton.icon(
                onPressed: _selectedAction != null && _canTrade
                    ? _previewSelected
                    : null,
                icon: _busy
                    ? const SizedBox(
                        width: 14,
                        height: 14,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.arrow_forward_rounded, size: 17),
                label: Text(_busy ? 'Working…' : 'Preview Order'),
              ),
            ],
          ),
          if (_status != null) ...[
            const SizedBox(height: Gap.sm),
            Text(
              _status!,
              style: AppText.caption.copyWith(
                color: _statusError ? kNegative : kPositive,
              ),
            ),
          ],
        ],
      ),
    );
  }

  static _SetupSpec _setupSpec(String id) {
    for (final group in _setupGroups) {
      for (final setup in group.setups) {
        if (setup.id == id) return setup;
      }
    }
    return _SetupSpec(id, id.replaceAll('_', ' '), 'Server-qualified setup');
  }

  static _StrategySpec _strategy(String action) => _strategies.firstWhere(
    (item) => item.action == action,
    orElse: () => _StrategySpec(
      action: action,
      title: action.replaceAll('_', ' '),
      detail: '',
      badge: '—',
      buy: true,
    ),
  );
}

class _CommandStrip extends StatelessWidget {
  const _CommandStrip({
    required this.modeLabel,
    required this.message,
    required this.error,
    required this.ready,
  });

  final String modeLabel;
  final String message;
  final bool error;
  final bool ready;

  @override
  Widget build(BuildContext context) {
    final tone = error
        ? kNegative
        : ready
        ? kPositive
        : kWarning;
    return Container(
      padding: const EdgeInsets.all(Gap.md),
      decoration: BoxDecoration(
        color: tone.withValues(alpha: .08),
        borderRadius: BorderRadius.circular(Radii.md),
        border: Border.all(color: tone.withValues(alpha: .48)),
      ),
      child: Row(
        children: [
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(
              color: tone,
              shape: BoxShape.circle,
              boxShadow: [BoxShadow(color: tone, blurRadius: 8)],
            ),
          ),
          const SizedBox(width: Gap.sm),
          Text(modeLabel, style: AppText.kicker),
          const SizedBox(width: Gap.md),
          Expanded(
            child: Text(
              message,
              textAlign: TextAlign.right,
              style: AppText.caption.copyWith(color: tone),
            ),
          ),
        ],
      ),
    );
  }
}

class _StateTile extends StatelessWidget {
  const _StateTile({
    required this.label,
    required this.value,
    required this.detail,
    required this.tone,
    this.trailing,
  });

  final String label;
  final String value;
  final String detail;
  final Color tone;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    return Container(
      constraints: const BoxConstraints(minHeight: 92),
      padding: const EdgeInsets.all(Gap.md),
      decoration: BoxDecoration(
        gradient: LinearGradient(
          colors: [
            tone.withValues(alpha: .12),
            Theme.of(context).colorScheme.surface.withValues(alpha: .9),
          ],
        ),
        borderRadius: BorderRadius.circular(Radii.md),
        border: Border.all(color: tone.withValues(alpha: .38)),
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Text(label.toUpperCase(), style: AppText.kicker),
                const SizedBox(height: Gap.xs),
                Text(value, style: AppText.title.copyWith(color: tone)),
                const SizedBox(height: 2),
                Text(
                  detail,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: AppText.caption.copyWith(
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                  ),
                ),
              ],
            ),
          ),
          if (trailing != null) ...[const SizedBox(width: Gap.sm), trailing!],
        ],
      ),
    );
  }
}

class _SetupTile extends StatelessWidget {
  const _SetupTile({
    required this.spec,
    required this.state,
    required this.selected,
    required this.onTap,
  });

  final _SetupSpec spec;
  final Map<String, dynamic>? state;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final eligible = state?['eligible'] == true;
    // The server flags its ungated override setup. Tone it as a warning
    // rather than a green confirmation so an operator can never mistake it
    // for engine-backed evidence.
    final override = state?['override'] == true;
    final tone = override
        ? kWarning
        : eligible
        ? kPositive
        : kNegative;
    final detail = '${state?['detail'] ?? spec.fallbackDetail}';
    return Semantics(
      button: eligible,
      selected: selected,
      enabled: eligible,
      label:
          '${spec.label}, ${override
              ? 'ungated'
              : eligible
              ? 'eligible'
              : 'blocked'}',
      child: InkWell(
        onTap: eligible ? onTap : null,
        borderRadius: BorderRadius.circular(Radii.md),
        child: AnimatedContainer(
          duration: Motion.fast,
          constraints: const BoxConstraints(minHeight: 62),
          padding: const EdgeInsets.all(Gap.md),
          decoration: BoxDecoration(
            color: tone.withValues(alpha: selected ? .18 : .07),
            borderRadius: BorderRadius.circular(Radii.md),
            border: Border.all(
              color: tone.withValues(alpha: selected ? .9 : .58),
              width: selected ? 1.5 : 1,
            ),
            boxShadow: selected
                ? [
                    BoxShadow(
                      color: tone.withValues(alpha: .18),
                      blurRadius: 15,
                    ),
                  ]
                : null,
          ),
          child: Row(
            children: [
              _SelectionDot(selected: selected, tone: tone),
              const SizedBox(width: Gap.sm),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      spec.label,
                      style: AppText.body.copyWith(fontWeight: FontWeight.w700),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      detail,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: AppText.caption.copyWith(
                        color: Theme.of(context).colorScheme.onSurfaceVariant,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: Gap.sm),
              Text(
                override
                    ? 'UNGATED'
                    : eligible
                    ? 'ELIGIBLE'
                    : 'BLOCKED',
                style: AppText.kicker.copyWith(color: tone, fontSize: 7.5),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _SelectionDot extends StatelessWidget {
  const _SelectionDot({required this.selected, required this.tone});

  final bool selected;
  final Color tone;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 16,
      height: 16,
      padding: const EdgeInsets.all(3),
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        border: Border.all(color: tone.withValues(alpha: .8)),
      ),
      child: selected
          ? DecoratedBox(
              decoration: BoxDecoration(color: tone, shape: BoxShape.circle),
            )
          : null,
    );
  }
}

class _SelectedSetupBanner extends StatelessWidget {
  const _SelectedSetupBanner({
    required this.title,
    required this.detail,
    this.tone = kPositive,
  });

  final String title;
  final String detail;
  final Color tone;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(Gap.md),
      decoration: BoxDecoration(
        color: tone.withValues(alpha: .10),
        borderRadius: BorderRadius.circular(Radii.md),
        border: Border(left: BorderSide(color: tone, width: 3)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('SELECTED SETUP', style: AppText.kicker.copyWith(color: tone)),
          const SizedBox(height: Gap.xs),
          Text(title, style: AppText.title),
          Text(
            detail,
            style: AppText.caption.copyWith(
              color: Theme.of(context).colorScheme.onSurfaceVariant,
            ),
          ),
        ],
      ),
    );
  }
}

class _StrategySection extends StatelessWidget {
  const _StrategySection({
    required this.title,
    required this.subtitle,
    required this.tone,
    required this.strategies,
    required this.allowed,
    required this.selected,
    required this.canTrade,
    required this.onSelected,
  });

  final String title;
  final String subtitle;
  final Color tone;
  final List<_StrategySpec> strategies;
  final Set<String> allowed;
  final String? selected;
  final bool canTrade;
  final ValueChanged<String> onSelected;

  @override
  Widget build(BuildContext context) {
    return Container(
      clipBehavior: Clip.antiAlias,
      decoration: BoxDecoration(
        color: tone.withValues(alpha: .05),
        borderRadius: BorderRadius.circular(Radii.md),
        border: Border.all(color: tone.withValues(alpha: .75)),
      ),
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.symmetric(
              horizontal: Gap.md,
              vertical: Gap.sm,
            ),
            child: Row(
              children: [
                Text(title, style: AppText.kicker.copyWith(color: tone)),
                const SizedBox(width: Gap.md),
                Expanded(
                  child: Text(
                    subtitle,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    textAlign: TextAlign.right,
                    style: AppText.caption.copyWith(
                      color: Theme.of(context).colorScheme.onSurfaceVariant,
                    ),
                  ),
                ),
              ],
            ),
          ),
          Divider(height: 1, color: tone.withValues(alpha: .32)),
          for (var index = 0; index < strategies.length; index++) ...[
            _StrategyTile(
              spec: strategies[index],
              tone: tone,
              enabled: canTrade && allowed.contains(strategies[index].action),
              selected: selected == strategies[index].action,
              onTap: () => onSelected(strategies[index].action),
            ),
            if (index != strategies.length - 1)
              Divider(height: 1, color: tone.withValues(alpha: .22)),
          ],
        ],
      ),
    );
  }
}

class _StrategyTile extends StatelessWidget {
  const _StrategyTile({
    required this.spec,
    required this.tone,
    required this.enabled,
    required this.selected,
    required this.onTap,
  });

  final _StrategySpec spec;
  final Color tone;
  final bool enabled;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: enabled,
      enabled: enabled,
      selected: selected,
      child: InkWell(
        onTap: enabled ? onTap : null,
        child: AnimatedOpacity(
          duration: Motion.fast,
          opacity: enabled ? 1 : .46,
          child: AnimatedContainer(
            duration: Motion.fast,
            constraints: const BoxConstraints(minHeight: 62),
            padding: const EdgeInsets.all(Gap.md),
            color: selected ? tone.withValues(alpha: .14) : Colors.transparent,
            child: Row(
              children: [
                _SelectionDot(selected: selected, tone: tone),
                const SizedBox(width: Gap.md),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(spec.title, style: AppText.title),
                      const SizedBox(height: 2),
                      Text(
                        spec.detail,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: AppText.caption.copyWith(
                          color: Theme.of(context).colorScheme.onSurfaceVariant,
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(width: Gap.sm),
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 8,
                    vertical: 4,
                  ),
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(Radii.pill),
                    border: Border.all(color: tone.withValues(alpha: .7)),
                  ),
                  child: Text(
                    spec.badge,
                    style: AppText.kicker.copyWith(color: tone, fontSize: 8),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _SafetyStrip extends StatelessWidget {
  const _SafetyStrip({required this.dryRun});

  final bool dryRun;

  @override
  Widget build(BuildContext context) {
    final items = [
      const (Icons.event_available_rounded, 'Nearest operational expiry'),
      const (Icons.bolt_rounded, 'Fresh executable quote'),
      (
        Icons.account_balance_wallet_outlined,
        dryRun ? 'Configured simulation lots' : 'Wallet-affordable lots',
      ),
      (
        Icons.shield_outlined,
        dryRun
            ? 'DRY RUN protection starts at entry'
            : 'LIVE protection starts after fill',
      ),
    ];
    return Wrap(
      spacing: Gap.sm,
      runSpacing: Gap.sm,
      children: [
        for (final item in items)
          Chip(
            avatar: Icon(
              item.$1,
              size: 15,
              color: Theme.of(context).colorScheme.primary,
            ),
            label: Text(item.$2),
            side: BorderSide(color: Theme.of(context).colorScheme.outline),
            backgroundColor: Theme.of(
              context,
            ).colorScheme.surface.withValues(alpha: .82),
            labelStyle: AppText.caption,
          ),
      ],
    );
  }
}

class _PreviewLine extends StatelessWidget {
  const _PreviewLine(this.label, this.value);

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return StatRow(label, value, mono: false);
  }
}

double? _number(dynamic value) {
  if (value is num) return value.toDouble();
  return double.tryParse('$value');
}
