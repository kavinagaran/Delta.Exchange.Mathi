library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../theme/design.dart';
import '../widgets/kit.dart';

class LogsScreen extends StatefulWidget {
  const LogsScreen({
    super.key,
    required this.api,
    required this.onUnauthorised,
  });

  final DashboardApi api;
  final VoidCallback onUnauthorised;

  @override
  State<LogsScreen> createState() => _LogsScreenState();
}

class _LogsScreenState extends State<LogsScreen> {
  List<String> _lines = const [];
  String _source = '';
  String _message = '';
  String? _error;
  int _limit = 100;
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
    final result = await widget.api.logs(limit: _limit);
    if (!mounted) return;
    if (result.unauthorised) {
      widget.onUnauthorised();
      return;
    }
    final data = result.data;
    setState(() {
      _loading = false;
      _lines = (data?['lines'] as List<dynamic>? ?? const [])
          .map((line) => '$line')
          .toList()
          .reversed
          .toList();
      _source = '${data?['source'] ?? ''}';
      _message = '${data?['message'] ?? ''}';
      _error = result.ok ? null : result.error;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (_loading && _lines.isEmpty) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    }
    return RefreshIndicator(
      onRefresh: _refresh,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.md, Gap.lg, Gap.xxl),
        children: [
          const PageIntro(
            icon: Icons.receipt_long_rounded,
            title: 'Activity Log',
            subtitle: 'Orders, decisions, protection and safety events.',
          ),
          const SizedBox(height: Gap.md),
          AppCard(
            kicker: 'Activity',
            title: _source == 'account_activity' ? 'Account events' : 'Bot log',
            trailing: DropdownButton<int>(
              value: _limit,
              underline: const SizedBox.shrink(),
              items: const [
                DropdownMenuItem(value: 50, child: Text('50')),
                DropdownMenuItem(value: 100, child: Text('100')),
                DropdownMenuItem(value: 250, child: Text('250')),
              ],
              onChanged: (value) {
                if (value == null) return;
                setState(() => _limit = value);
                _refresh();
              },
            ),
            child: Text(
              _error ?? _message,
              style: AppText.caption.copyWith(
                color: _error == null
                    ? Theme.of(context).colorScheme.onSurfaceVariant
                    : kNegative,
              ),
            ),
          ),
          const SizedBox(height: Gap.md),
          if (_lines.isEmpty)
            const AppCard(
              child: StatePlaceholder(
                icon: Icons.receipt_long_outlined,
                message: 'No activity yet.',
              ),
            )
          else
            for (final line in _lines) ...[
              _LogEvent(line: line),
              const SizedBox(height: Gap.sm),
            ],
        ],
      ),
    );
  }
}

class _LogEvent extends StatelessWidget {
  const _LogEvent({required this.line});

  final String line;

  @override
  Widget build(BuildContext context) {
    final upper = line.toUpperCase();
    final tone = upper.contains('OPENED') || upper.contains('CONNECTED')
        ? kPositive
        : upper.contains('WARNING') || upper.contains('HOLD')
        ? kWarning
        : upper.contains('ERROR') ||
              upper.contains('FAILED') ||
              upper.contains('REJECT')
        ? kNegative
        : Theme.of(context).colorScheme.primary;
    final parts = line.split(' · ');
    final time = parts.isNotEmpty ? parts.first : '';
    final event = parts.length > 1 ? parts[1] : 'ACTIVITY';
    final details = parts.length > 2 ? parts.skip(2).join(' · ') : '';
    return AppCard(
      padding: const EdgeInsets.all(Gap.md),
      accent: tone,
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 34,
            height: 34,
            decoration: BoxDecoration(
              color: tone.withValues(alpha: .13),
              shape: BoxShape.circle,
            ),
            child: Icon(Icons.bolt_rounded, size: 18, color: tone),
          ),
          const SizedBox(width: Gap.md),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(event, style: AppText.body.copyWith(color: tone)),
                if (details.isNotEmpty) Text(details, style: AppText.caption),
                const SizedBox(height: 2),
                Text(
                  time,
                  style: AppText.caption.copyWith(
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                    fontSize: 9.5,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
