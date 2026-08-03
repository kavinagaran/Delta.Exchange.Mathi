library;

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../theme/design.dart';
import '../widgets/kit.dart';

class AccountsScreen extends StatefulWidget {
  const AccountsScreen({
    super.key,
    required this.api,
    required this.onUnauthorised,
  });

  final DashboardApi api;
  final VoidCallback onUnauthorised;

  @override
  State<AccountsScreen> createState() => _AccountsScreenState();
}

class _AccountsScreenState extends State<AccountsScreen> {
  List<Map<String, dynamic>> _accounts = const [];
  Map<String, dynamic> _bots = const {};
  bool _loading = true;
  String? _error;
  String? _busyAccount;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  Future<void> _refresh() async {
    if (mounted) setState(() => _loading = true);
    final results = await Future.wait([
      widget.api.accounts(),
      widget.api.bots(),
    ]);
    if (!mounted) return;
    if (results.any((result) => result.unauthorised)) {
      widget.onUnauthorised();
      return;
    }
    setState(() {
      _loading = false;
      _accounts = (results[0].data as List<dynamic>? ?? const [])
          .whereType<Map<String, dynamic>>()
          .toList();
      _bots = results[1].data as Map<String, dynamic>? ?? const {};
      _error = results[0].ok ? null : results[0].error;
    });
  }

  Future<void> _toggleBot(String username, bool active) async {
    setState(() => _busyAccount = username);
    final result = await widget.api.setBotActive(username, !active);
    if (!mounted) return;
    setState(() => _busyAccount = null);
    _toast(
      result.ok
          ? 'Bot ${active ? 'stopped' : 'started'}'
          : result.error ?? 'Action failed',
      result.ok,
    );
    if (result.ok) await _refresh();
  }

  Future<void> _test(Map<String, dynamic> account) async {
    final username = '${account['username']}';
    setState(() => _busyAccount = username);
    final result = await widget.api.testAccount({'username': username});
    if (!mounted) return;
    setState(() => _busyAccount = null);
    final data = result.data;
    final balance = data is Map ? data['usd_balance'] : null;
    _toast(
      result.ok
          ? 'Connected${balance == null ? '' : ' · \$$balance'}'
          : result.error ?? 'Connection failed',
      result.ok,
    );
  }

  Future<void> _delete(Map<String, dynamic> account) async {
    final username = '${account['username']}';
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Remove $username?'),
        content: const Text('Trade history stays on the server.'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Remove'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    setState(() => _busyAccount = username);
    final result = await widget.api.deleteAccount(username);
    if (!mounted) return;
    setState(() => _busyAccount = null);
    _toast(
      result.ok ? 'Account removed' : result.error ?? 'Remove failed',
      result.ok,
    );
    if (result.ok) await _refresh();
  }

  Future<void> _edit([Map<String, dynamic>? account]) async {
    final username = TextEditingController(
      text: '${account?['username'] ?? ''}',
    );
    final name = TextEditingController(
      text: '${account?['display_name'] ?? ''}',
    );
    final password = TextEditingController();
    final key = TextEditingController();
    final secret = TextEditingController();
    var saving = false;
    String? error;
    final changed = await showModalBottomSheet<bool>(
      context: context,
      useSafeArea: true,
      isScrollControlled: true,
      backgroundColor: Theme.of(context).colorScheme.surface,
      builder: (sheetContext) => StatefulBuilder(
        builder: (context, setSheetState) {
          Future<void> save() async {
            if (username.text.trim().isEmpty) {
              setSheetState(() => error = 'Username is required.');
              return;
            }
            setSheetState(() {
              saving = true;
              error = null;
            });
            final result = await widget.api.saveAccount({
              'username': username.text.trim(),
              'display_name': name.text.trim(),
              if (password.text.isNotEmpty) 'password': password.text,
              if (key.text.isNotEmpty) 'api_key': key.text.trim(),
              if (secret.text.isNotEmpty) 'api_secret': secret.text.trim(),
            });
            if (!sheetContext.mounted) return;
            if (result.ok) {
              Navigator.pop(sheetContext, true);
            } else {
              setSheetState(() {
                saving = false;
                error = result.error;
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
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Row(
                    children: [
                      const Icon(Icons.manage_accounts_rounded),
                      const SizedBox(width: Gap.sm),
                      Expanded(
                        child: Text(
                          account == null ? 'Add account' : 'Edit account',
                          style: AppText.title,
                        ),
                      ),
                      IconButton(
                        onPressed: () => Navigator.pop(context, false),
                        icon: const Icon(Icons.close_rounded),
                      ),
                    ],
                  ),
                  const SizedBox(height: Gap.md),
                  TextField(
                    controller: username,
                    enabled: account == null,
                    decoration: const InputDecoration(labelText: 'Username'),
                  ),
                  const SizedBox(height: Gap.sm),
                  TextField(
                    controller: name,
                    decoration: const InputDecoration(
                      labelText: 'Display name',
                    ),
                  ),
                  const SizedBox(height: Gap.sm),
                  TextField(
                    controller: password,
                    obscureText: true,
                    decoration: InputDecoration(
                      labelText: account == null ? 'Password' : 'New password',
                    ),
                  ),
                  const SizedBox(height: Gap.sm),
                  TextField(
                    controller: key,
                    obscureText: true,
                    decoration: const InputDecoration(
                      labelText: 'Delta API key',
                    ),
                  ),
                  const SizedBox(height: Gap.sm),
                  TextField(
                    controller: secret,
                    obscureText: true,
                    decoration: const InputDecoration(
                      labelText: 'Delta API secret',
                    ),
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
                            dimension: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.check_rounded),
                    label: Text(saving ? 'Saving…' : 'Save account'),
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
    username.dispose();
    name.dispose();
    password.dispose();
    key.dispose();
    secret.dispose();
    if (changed == true) {
      _toast('Account saved', true);
      await _refresh();
    }
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
    if (_loading && _accounts.isEmpty) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    }
    return RefreshIndicator(
      onRefresh: _refresh,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(Gap.lg, Gap.md, Gap.lg, Gap.xxl),
        children: [
          const PageIntro(
            icon: Icons.manage_accounts_rounded,
            title: 'API Accounts',
            subtitle: 'Users, exchange access and bot availability.',
          ),
          const SizedBox(height: Gap.md),
          AppCard(
            kicker: 'Accounts',
            title: '${_accounts.length} connected users',
            trailing: CompactAction(
              label: 'Add',
              icon: Icons.add_rounded,
              onPressed: _edit,
              filled: true,
            ),
            child: Text(
              _error ?? 'Credentials stay on your server.',
              style: AppText.caption.copyWith(
                color: _error == null
                    ? Theme.of(context).colorScheme.onSurfaceVariant
                    : kNegative,
              ),
            ),
          ),
          const SizedBox(height: Gap.md),
          for (final account in _accounts) ...[
            _AccountCard(
              account: account,
              bot: _bots['${account['username']}'] is Map
                  ? Map<String, dynamic>.from(
                      _bots['${account['username']}'] as Map,
                    )
                  : const {},
              busy: _busyAccount == '${account['username']}',
              onEdit: () => _edit(account),
              onTest: () => _test(account),
              onToggle: (active) =>
                  _toggleBot('${account['username']}', active),
              onDelete: () => _delete(account),
            ),
            const SizedBox(height: Gap.md),
          ],
        ],
      ),
    );
  }
}

class _AccountCard extends StatelessWidget {
  const _AccountCard({
    required this.account,
    required this.bot,
    required this.busy,
    required this.onEdit,
    required this.onTest,
    required this.onToggle,
    required this.onDelete,
  });

  final Map<String, dynamic> account;
  final Map<String, dynamic> bot;
  final bool busy;
  final VoidCallback onEdit;
  final VoidCallback onTest;
  final ValueChanged<bool> onToggle;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    final active = bot['active'] == true;
    final primary = account['primary'] == true;
    return AppCard(
      kicker: primary ? 'Primary' : 'Account',
      title: '${account['display_name'] ?? account['username']}',
      accent: active ? kPositive : kNeutral,
      trailing: StatusPill(
        active ? 'RUNNING' : 'STOPPED',
        colour: active ? kPositive : kNeutral,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          StatRow('Username', '${account['username']}'),
          StatRow('API key', '${account['api_key'] ?? 'Not set'}'),
          const SizedBox(height: Gap.md),
          if (busy)
            const LinearProgressIndicator(minHeight: 2)
          else
            Wrap(
              spacing: Gap.sm,
              runSpacing: Gap.sm,
              children: [
                CompactAction(
                  label: 'Edit',
                  icon: Icons.edit_outlined,
                  onPressed: onEdit,
                ),
                CompactAction(
                  label: 'Test',
                  icon: Icons.wifi_tethering_rounded,
                  tone: kPositive,
                  onPressed: onTest,
                ),
                if (bot['supported'] == true)
                  CompactAction(
                    label: active ? 'Stop bot' : 'Start bot',
                    icon: active
                        ? Icons.stop_rounded
                        : Icons.play_arrow_rounded,
                    tone: active ? kNegative : kPositive,
                    onPressed: () => onToggle(active),
                  ),
                if (!primary)
                  CompactAction(
                    label: 'Remove',
                    icon: Icons.delete_outline_rounded,
                    tone: kNegative,
                    onPressed: onDelete,
                  ),
              ],
            ),
        ],
      ),
    );
  }
}
