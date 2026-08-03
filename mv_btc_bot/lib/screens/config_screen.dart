library;

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../theme/design.dart';
import '../widgets/kit.dart';

class ConfigScreen extends StatefulWidget {
  const ConfigScreen({
    super.key,
    required this.api,
    required this.onUnauthorised,
    required this.displayName,
  });

  final DashboardApi api;
  final VoidCallback onUnauthorised;
  final String displayName;

  @override
  State<ConfigScreen> createState() => _ConfigScreenState();
}

class _ConfigScreenState extends State<ConfigScreen> {
  static const _numericSections = <String, List<_FieldSpec>>{
    'Position protection': [
      _FieldSpec('TREND_TP_PREMIUM_PCT', 'Take profit', suffix: '%'),
      _FieldSpec('TREND_SL_PREMIUM_PCT', 'Stop loss', suffix: '%'),
      _FieldSpec('TREND_TSL_ARM_PREMIUM_PCT', 'TSL arm', suffix: '%'),
      _FieldSpec('TREND_TSL_TRAIL_PREMIUM_PCT', 'TSL trail', suffix: '%'),
      _FieldSpec('TP_POLL_SECS_TREND', 'Check interval', suffix: 'sec'),
    ],
    'Risk limits': [
      _FieldSpec('TREND_RISK_BUDGET_USD', 'Risk per trade', prefix: '\$'),
      _FieldSpec('TREND_DRY_RUN_CAPITAL_USD', 'Paper capital', prefix: '\$'),
      _FieldSpec('SHORT_MAX_RISK_USD', 'MOVE max loss', prefix: '\$'),
      _FieldSpec('MAX_TRADES_PER_DAY_GLOBAL', 'Trades per day'),
      _FieldSpec('MAX_DAILY_LOSS_USD', 'Daily loss', prefix: '\$'),
      _FieldSpec('MAX_OPEN_RISK_USD', 'Open risk', prefix: '\$'),
      _FieldSpec(
        'MAX_ACCOUNT_PREMIUM_AT_RISK_USD',
        'Premium risk',
        prefix: '\$',
      ),
      _FieldSpec('MAX_CONSECUTIVE_LOSSES', 'Losses before pause'),
      _FieldSpec('LOSS_COOLDOWN_MINUTES', 'Cooldown', suffix: 'min'),
    ],
    'Execution': [
      _FieldSpec('TREND_MAX_SPREAD_PCT', 'CE / PE spread', suffix: '%'),
      _FieldSpec('TREND_MAX_SLIPPAGE_PCT', 'CE / PE slippage', suffix: '%'),
      _FieldSpec(
        'TREND_QUOTE_MAX_AGE_SECS',
        'CE / PE quote age',
        suffix: 'sec',
      ),
      _FieldSpec('MAX_SPREAD_PCT', 'MOVE spread', suffix: '%'),
      _FieldSpec('MAX_SLIPPAGE_PCT', 'MOVE slippage', suffix: '%'),
      _FieldSpec('MAX_QUOTE_AGE_SEC', 'MOVE quote age', suffix: 'sec'),
    ],
  };

  final Map<String, TextEditingController> _fields = {};
  Map<String, dynamic>? _config;
  Map<String, dynamic>? _availability;
  Map<String, dynamic>? _controller;
  String _accountMode = 'true';
  String _automation = 'disabled';
  bool _riskFailClosed = true;
  bool _allowExternal = false;
  bool _safeExecution = true;
  bool _telegram = false;
  bool _loading = true;
  bool _saving = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    for (final field in _fields.values) {
      field.dispose();
    }
    super.dispose();
  }

  Future<void> _load() async {
    if (mounted) setState(() => _loading = true);
    final results = await Future.wait([
      widget.api.config(),
      widget.api.tradingMode(),
      widget.api.scoreAutoStatus(),
    ]);
    if (!mounted) return;
    if (results.any((result) => result.unauthorised)) {
      widget.onUnauthorised();
      return;
    }
    final config = results[0].data;
    if (config == null) {
      setState(() {
        _loading = false;
        _error = results[0].error ?? 'Configuration unavailable';
      });
      return;
    }
    for (final spec in _numericSections.values.expand((value) => value)) {
      _fields.putIfAbsent(spec.key, TextEditingController.new).text =
          '${config[spec.key] ?? ''}';
    }
    for (final key in [
      'TREND_SCORE_AUTO_LOTS',
      'TELEGRAM_BOT_TOKEN',
      'TELEGRAM_CHAT_ID',
    ]) {
      _fields.putIfAbsent(key, TextEditingController.new).text =
          '${config[key] ?? ''}';
    }
    setState(() {
      _config = config;
      _availability = results[1].data;
      _controller = results[2].data;
      _accountMode = _truth(config['DRY_RUN']) ? 'true' : 'false';
      final automation =
          '${config['TREND_ENGINE_SCORE_AUTO_MODE'] ?? 'disabled'}'
              .toLowerCase();
      _automation = const {'disabled', 'dry_run', 'live'}.contains(automation)
          ? automation
          : 'disabled';
      _riskFailClosed = _truth(config['RISK_FAIL_CLOSED'], fallback: true);
      _allowExternal = _truth(config['ALLOW_EXTERNAL_POSITIONS_WITH_BOT']);
      _safeExecution = _truth(config['SAFE_EXECUTION_ENABLED'], fallback: true);
      _telegram = _truth(config['TELEGRAM_ALERTS']);
      _loading = false;
      _error = null;
    });
  }

  Future<void> _save() async {
    if (_saving || _config == null) return;
    final payload = <String, dynamic>{
      'DRY_RUN': _accountMode,
      'TREND_ENGINE_SCORE_AUTO_MODE': _automation,
      'RISK_FAIL_CLOSED': '$_riskFailClosed',
      'ALLOW_EXTERNAL_POSITIONS_WITH_BOT': '$_allowExternal',
      'SAFE_EXECUTION_ENABLED': '$_safeExecution',
      'TELEGRAM_ALERTS': '$_telegram',
      for (final entry in _fields.entries) entry.key: entry.value.text.trim(),
    };
    setState(() => _saving = true);
    final result = await widget.api.saveConfig(payload);
    if (!mounted) return;
    setState(() => _saving = false);
    _toast(
      result.ok ? 'Configuration saved' : result.error ?? 'Save failed',
      result.ok,
    );
    if (result.ok) await _load();
  }

  Future<void> _resetLock() async {
    final result = await widget.api.resetZoneLock();
    if (!mounted) return;
    _toast(
      result.ok ? 'Zone lock reset' : result.error ?? 'Reset failed',
      result.ok,
    );
    if (result.ok) await _load();
  }

  Future<void> _testTelegram() async {
    final result = await widget.api.testTelegram();
    if (!mounted) return;
    _toast(
      result.ok ? 'Telegram test sent' : result.error ?? 'Test failed',
      result.ok,
    );
  }

  void _toast(String message, bool ok) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(message),
        backgroundColor: ok ? kPositive : kNegative,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_loading && _config == null) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    }
    if (_error != null && _config == null) {
      return StatePlaceholder(
        icon: Icons.tune_rounded,
        message: 'Configuration unavailable',
        detail: _error,
        onRetry: _load,
        tone: kNegative,
      );
    }
    final lock = _controller?['setup_lock'];
    final lockMap = lock is Map<String, dynamic>
        ? lock
        : const <String, dynamic>{};
    final lockActive = lockMap['active'] == true;
    final modeAllowed = _availability?['mode_change_allowed'] == true;

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.md, Gap.lg, 110),
        children: [
          PageIntro(
            icon: Icons.tune_rounded,
            title: 'Bot Config',
            subtitle: "Configure ${widget.displayName}'s Trend Engine.",
          ),
          const SizedBox(height: Gap.md),
          AppCard(
            kicker: 'Trend engine',
            title: '${widget.displayName} configuration',
            accent: Theme.of(context).colorScheme.primary,
            trailing: StatusPill(
              _automation == 'live'
                  ? 'LIVE'
                  : _automation == 'dry_run'
                  ? 'DRY RUN'
                  : 'OFF',
              colour: _automation == 'live'
                  ? kPositive
                  : _automation == 'dry_run'
                  ? kWarning
                  : kNeutral,
            ),
            child: Column(
              children: [
                DropdownButtonFormField<String>(
                  initialValue: _accountMode,
                  decoration: const InputDecoration(labelText: 'Trading mode'),
                  items: const [
                    DropdownMenuItem(value: 'true', child: Text('DRY RUN')),
                    DropdownMenuItem(value: 'false', child: Text('LIVE')),
                  ],
                  onChanged: modeAllowed
                      ? (value) => setState(() => _accountMode = value!)
                      : null,
                ),
                if (!modeAllowed) ...[
                  const SizedBox(height: Gap.xs),
                  Align(
                    alignment: Alignment.centerLeft,
                    child: Text(
                      '${_availability?['mode_lock_reason'] ?? 'Mode locked'}',
                      style: AppText.caption.copyWith(color: kWarning),
                    ),
                  ),
                ],
                const SizedBox(height: Gap.sm),
                DropdownButtonFormField<String>(
                  initialValue: _automation,
                  decoration: const InputDecoration(
                    labelText: 'Automatic trading',
                  ),
                  items: const [
                    DropdownMenuItem(value: 'disabled', child: Text('OFF')),
                    DropdownMenuItem(value: 'dry_run', child: Text('DRY RUN')),
                    DropdownMenuItem(value: 'live', child: Text('LIVE')),
                  ],
                  onChanged: (value) => setState(() => _automation = value!),
                ),
                const SizedBox(height: Gap.sm),
                _ConfigInput(
                  label: 'Order size',
                  controller: _fields['TREND_SCORE_AUTO_LOTS']!,
                  suffix: 'lots',
                ),
                const SizedBox(height: Gap.md),
                const _ZoneBand(),
                const SizedBox(height: Gap.md),
                Row(
                  children: [
                    CompactAction(
                      label: 'Reset Zone Lock',
                      icon: Icons.lock_reset_rounded,
                      onPressed: lockActive ? _resetLock : null,
                      filled: lockActive,
                    ),
                    const SizedBox(width: Gap.sm),
                    Expanded(
                      child: Text(
                        lockActive
                            ? '${lockMap['zone'] ?? 'Zone'} locked'
                            : 'No active lock',
                        style: AppText.caption.copyWith(
                          color: Theme.of(context).colorScheme.onSurfaceVariant,
                        ),
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
          for (final section in _numericSections.entries) ...[
            const SizedBox(height: Gap.md),
            AppCard(
              title: section.key,
              child: LayoutBuilder(
                builder: (context, constraints) {
                  final wide = constraints.maxWidth > 580;
                  return Wrap(
                    spacing: Gap.sm,
                    runSpacing: Gap.sm,
                    children: [
                      for (final spec in section.value)
                        SizedBox(
                          width: wide
                              ? (constraints.maxWidth - Gap.sm) / 2
                              : constraints.maxWidth,
                          child: _ConfigInput(
                            label: spec.label,
                            controller: _fields[spec.key]!,
                            prefix: spec.prefix,
                            suffix: spec.suffix,
                          ),
                        ),
                    ],
                  );
                },
              ),
            ),
          ],
          const SizedBox(height: Gap.md),
          AppCard(
            title: 'Safety',
            child: Column(
              children: [
                SwitchListTile.adaptive(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('Fail closed', style: AppText.body),
                  value: _riskFailClosed,
                  onChanged: (value) => setState(() => _riskFailClosed = value),
                ),
                SwitchListTile.adaptive(
                  contentPadding: EdgeInsets.zero,
                  title: const Text(
                    'Allow external positions',
                    style: AppText.body,
                  ),
                  value: _allowExternal,
                  onChanged: (value) => setState(() => _allowExternal = value),
                ),
                SwitchListTile.adaptive(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('Safe IOC execution', style: AppText.body),
                  value: _safeExecution,
                  onChanged: (value) => setState(() => _safeExecution = value),
                ),
              ],
            ),
          ),
          const SizedBox(height: Gap.md),
          AppCard(
            title: 'Telegram',
            child: Column(
              children: [
                SwitchListTile.adaptive(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('Alerts', style: AppText.body),
                  value: _telegram,
                  onChanged: (value) => setState(() => _telegram = value),
                ),
                _ConfigInput(
                  label: 'Bot token',
                  controller: _fields['TELEGRAM_BOT_TOKEN']!,
                  obscure: true,
                ),
                const SizedBox(height: Gap.sm),
                _ConfigInput(
                  label: 'Chat ID',
                  controller: _fields['TELEGRAM_CHAT_ID']!,
                ),
                const SizedBox(height: Gap.md),
                Align(
                  alignment: Alignment.centerLeft,
                  child: CompactAction(
                    label: 'Send test',
                    icon: Icons.send_rounded,
                    onPressed: _testTelegram,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: Gap.lg),
          FilledButton.icon(
            onPressed: _saving ? null : _save,
            icon: _saving
                ? const SizedBox.square(
                    dimension: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.save_rounded),
            label: Text(_saving ? 'Saving…' : 'Save configuration'),
          ),
        ],
      ),
    );
  }
}

class _FieldSpec {
  const _FieldSpec(this.key, this.label, {this.prefix, this.suffix});
  final String key;
  final String label;
  final String? prefix;
  final String? suffix;
}

class _ConfigInput extends StatelessWidget {
  const _ConfigInput({
    required this.label,
    required this.controller,
    this.prefix,
    this.suffix,
    this.obscure = false,
  });
  final String label;
  final TextEditingController controller;
  final String? prefix;
  final String? suffix;
  final bool obscure;

  @override
  Widget build(BuildContext context) => TextField(
    controller: controller,
    obscureText: obscure,
    keyboardType: obscure
        ? TextInputType.text
        : const TextInputType.numberWithOptions(decimal: true),
    decoration: InputDecoration(
      labelText: label,
      prefixText: prefix,
      suffixText: suffix,
    ),
  );
}

class _ZoneBand extends StatelessWidget {
  const _ZoneBand();

  @override
  Widget build(BuildContext context) => Wrap(
    spacing: Gap.xs,
    runSpacing: Gap.xs,
    children: const [
      StatusPill('CE ≥ +40', colour: kZoneCall, dot: false),
      StatusPill('HOLD', colour: kZoneHold, dot: false),
      StatusPill('MV −30…+30', colour: kZoneMove, dot: false),
      StatusPill('HOLD', colour: kZoneHold, dot: false),
      StatusPill('PE ≤ −40', colour: kZonePut, dot: false),
    ],
  );
}

bool _truth(Object? value, {bool fallback = false}) {
  if (value == null) return fallback;
  if (value is bool) return value;
  return const {
    '1',
    'true',
    'yes',
    'on',
  }.contains('$value'.trim().toLowerCase());
}
