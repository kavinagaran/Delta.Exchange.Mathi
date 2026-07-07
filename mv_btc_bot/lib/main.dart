import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

// ─────────────────────────────────────────────────────────────
// Theme colors — matched to the web dashboard
// ─────────────────────────────────────────────────────────────
const kBg = Color(0xFF0A0E1A);
const kSurf = Color(0xFF111A2E);
const kBorder = Color(0xFF1E2A45);
const kGreen = Color(0xFF00FF87);
const kRed = Color(0xFFFF2D6E);
const kGold = Color(0xFFFFD700);
const kMuted = Color(0xFF8CA0BC);
const kText = Color(0xFFE8EEF7);

void main() {
  runApp(const MathiBotApp());
}

class MathiBotApp extends StatelessWidget {
  const MathiBotApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Mathi-bot',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: kBg,
        colorScheme: const ColorScheme.dark(
          primary: kGreen,
          surface: kSurf,
          error: kRed,
        ),
        appBarTheme: const AppBarTheme(
          backgroundColor: kBg,
          elevation: 0,
          centerTitle: false,
        ),
        cardTheme: const CardThemeData(
          color: kSurf,
          elevation: 0,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(14)),
            side: BorderSide(color: kBorder),
          ),
        ),
      ),
      home: const HomeShell(),
    );
  }
}

// ─────────────────────────────────────────────────────────────
// API client
// ─────────────────────────────────────────────────────────────
class Api {
  static String baseUrl = 'http://192.168.1.8:5001';

  static Future<void> loadBaseUrl() async {
    final prefs = await SharedPreferences.getInstance();
    baseUrl = prefs.getString('server_url') ?? baseUrl;
  }

  static Future<void> saveBaseUrl(String url) async {
    baseUrl = url.trim().replaceAll(RegExp(r'/+$'), '');
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('server_url', baseUrl);
  }

  static Future<dynamic> getJson(String path) async {
    final r = await http
        .get(Uri.parse('$baseUrl$path'))
        .timeout(const Duration(seconds: 8));
    return jsonDecode(r.body);
  }

  static Future<dynamic> postJson(String path, [Map<String, dynamic>? body]) async {
    final r = await http
        .post(
          Uri.parse('$baseUrl$path'),
          headers: {'Content-Type': 'application/json'},
          body: body == null ? null : jsonEncode(body),
        )
        .timeout(const Duration(seconds: 20));
    return jsonDecode(r.body);
  }
}

String fmtUsd(dynamic v, {int dp = 2}) {
  if (v == null) return '—';
  final n = (v as num).toDouble();
  final sign = n < 0 ? '-' : '';
  return '$sign\$${n.abs().toStringAsFixed(dp)}';
}

String fmtNum(dynamic v, {int dp = 0}) {
  if (v == null) return '—';
  return (v as num).toDouble().toStringAsFixed(dp);
}

// ─────────────────────────────────────────────────────────────
// Shell with bottom navigation
// ─────────────────────────────────────────────────────────────
class HomeShell extends StatefulWidget {
  const HomeShell({super.key});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _tab = 0;
  bool _ready = false;

  @override
  void initState() {
    super.initState();
    Api.loadBaseUrl().then((_) => setState(() => _ready = true));
  }

  @override
  Widget build(BuildContext context) {
    if (!_ready) {
      return const Scaffold(body: Center(child: CircularProgressIndicator(color: kGreen)));
    }
    return Scaffold(
      body: IndexedStack(
        index: _tab,
        children: const [DashboardPage(), LogsPage(), SettingsPage()],
      ),
      bottomNavigationBar: NavigationBar(
        backgroundColor: kSurf,
        indicatorColor: kGreen.withValues(alpha: 0.15),
        selectedIndex: _tab,
        onDestinationSelected: (i) => setState(() => _tab = i),
        destinations: const [
          NavigationDestination(icon: Icon(Icons.dashboard_outlined), selectedIcon: Icon(Icons.dashboard, color: kGreen), label: 'Dashboard'),
          NavigationDestination(icon: Icon(Icons.receipt_long_outlined), selectedIcon: Icon(Icons.receipt_long, color: kGreen), label: 'Logs'),
          NavigationDestination(icon: Icon(Icons.settings_outlined), selectedIcon: Icon(Icons.settings, color: kGreen), label: 'Settings'),
        ],
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────
// Dashboard page
// ─────────────────────────────────────────────────────────────
class DashboardPage extends StatefulWidget {
  const DashboardPage({super.key});

  @override
  State<DashboardPage> createState() => _DashboardPageState();
}

class _DashboardPageState extends State<DashboardPage> {
  Map<String, dynamic> _status = {};
  List<dynamic> _todayTrades = [];
  Map<String, dynamic> _tp = {};
  String? _error;
  Timer? _timer;
  double? _lastBtc;
  bool _btcUp = true;

  @override
  void initState() {
    super.initState();
    _refresh();
    _timer = Timer.periodic(const Duration(seconds: 10), (_) => _refresh());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _refresh() async {
    try {
      final results = await Future.wait([
        Api.getJson('/api/status'),
        Api.getJson('/api/today-trades'),
        Api.getJson('/api/tp-monitor'),
      ]);
      final st = results[0] as Map<String, dynamic>;
      final btc = (st['btc_futures_price'] as num?)?.toDouble();
      if (btc != null && _lastBtc != null && btc != _lastBtc) {
        _btcUp = btc > _lastBtc!;
      }
      if (btc != null) _lastBtc = btc;
      if (!mounted) return;
      setState(() {
        _status = st;
        _todayTrades = results[1] as List<dynamic>;
        _tp = results[2] as Map<String, dynamic>;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = 'Cannot reach bot server:\n${Api.baseUrl}\n\n$e');
    }
  }

  Future<void> _squareOff() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: kSurf,
        title: const Text('Square Off?'),
        content: const Text('Close the entire position now at market price?'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('SQUARE OFF', style: TextStyle(color: kRed, fontWeight: FontWeight.bold)),
          ),
        ],
      ),
    );
    if (ok != true) return;
    try {
      final d = await Api.postJson('/api/square-off');
      if (!mounted) return;
      final msg = d['ok'] == true
          ? 'Position closed  P&L: ${fmtUsd(d['pnl'])}'
          : 'Failed: ${d['error']}';
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(msg),
        backgroundColor: d['ok'] == true ? const Color(0xFF0A3524) : const Color(0xFF3A0F1E),
      ));
      _refresh();
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Error: $e')));
    }
  }

  Future<void> _toggleTp() async {
    final running = _tp['running'] == true;
    try {
      final d = await Api.postJson(running ? '/api/tp-monitor/stop' : '/api/tp-monitor/start');
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(d['ok'] == true
            ? (running ? 'TP monitor stopped' : 'TP monitor started')
            : 'Error: ${d['error']}'),
      ));
      _refresh();
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Error: $e')));
    }
  }

  Future<void> _editTpConfig() async {
    final targetCtl = TextEditingController(text: fmtNum(_tp['target_pnl'], dp: 0));
    final pollCtl = TextEditingController(text: fmtNum(_tp['poll_secs'], dp: 0));
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: kSurf,
        title: const Text('TP Monitor Config'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: targetCtl,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(labelText: 'Target P&L (\$)'),
            ),
            TextField(
              controller: pollCtl,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(labelText: 'Poll interval (s)'),
            ),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Save', style: TextStyle(color: kGold))),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await Api.postJson('/api/config', {
        'TP_TARGET_PNL': targetCtl.text,
        'TP_POLL_SECS': pollCtl.text,
      });
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('TP config saved')));
      _refresh();
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Error: $e')));
    }
  }

  @override
  Widget build(BuildContext context) {
    final st = _status;
    final open = st['status'] == 'OPEN';
    final btc = (st['btc_futures_price'] as num?)?.toDouble();
    final pnl = (st['live_pnl'] as num?)?.toDouble();

    return SafeArea(
      child: RefreshIndicator(
        color: kGreen,
        onRefresh: _refresh,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // ── Header ──
            Row(
              children: [
                const Text('⚡', style: TextStyle(fontSize: 20)),
                const SizedBox(width: 6),
                const Text('MATHI-BOT',
                    style: TextStyle(color: kGreen, fontSize: 22, fontWeight: FontWeight.w800, letterSpacing: 2)),
                const Spacer(),
                _StatusPill(status: st['status'] as String? ?? '...'),
              ],
            ),
            const SizedBox(height: 12),

            // ── BTC price capsule ──
            if (btc != null)
              Center(
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(30),
                    border: Border.all(color: _btcUp ? kGreen : kRed),
                    boxShadow: [
                      BoxShadow(
                        color: (_btcUp ? kGreen : kRed).withValues(alpha: 0.35),
                        blurRadius: 14,
                      ),
                    ],
                  ),
                  child: Text(
                    'BTC  \$${btc.toStringAsFixed(2)}  ${_btcUp ? '▲' : '▼'}',
                    style: TextStyle(
                      color: _btcUp ? kGreen : kRed,
                      fontWeight: FontWeight.w700,
                      fontSize: 16,
                      fontFeatures: const [FontFeature.tabularFigures()],
                    ),
                  ),
                ),
              ),
            const SizedBox(height: 16),

            if (_error != null)
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Text(_error!, style: const TextStyle(color: kRed)),
                ),
              ),

            // ── Position card ──
            Card(
              child: Padding(
                padding: const EdgeInsets.all(18),
                child: open ? _openPosition(st, pnl) : _noPosition(st),
              ),
            ),
            const SizedBox(height: 12),

            // ── TP monitor card ──
            if (open) ...[
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Row(
                    children: [
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            const Text('TAKE PROFIT MONITOR',
                                style: TextStyle(color: kMuted, fontSize: 10, letterSpacing: 1.2)),
                            const SizedBox(height: 4),
                            Text(
                              _tp['running'] == true ? '● Running' : '○ Stopped',
                              style: TextStyle(
                                color: _tp['running'] == true ? kGreen : kMuted,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                            Text(
                              'Target ${fmtUsd(_tp['target_pnl'], dp: 0)}  ·  every ${fmtNum(_tp['poll_secs'])}s',
                              style: const TextStyle(color: kMuted, fontSize: 12),
                            ),
                          ],
                        ),
                      ),
                      IconButton(
                        onPressed: _editTpConfig,
                        icon: const Icon(Icons.tune, color: kGold),
                      ),
                      FilledButton(
                        style: FilledButton.styleFrom(
                          backgroundColor: _tp['running'] == true
                              ? kRed.withValues(alpha: 0.15)
                              : kGreen.withValues(alpha: 0.15),
                          foregroundColor: _tp['running'] == true ? kRed : kGreen,
                        ),
                        onPressed: _toggleTp,
                        child: Text(_tp['running'] == true ? 'STOP' : 'START'),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
            ],

            // ── Today's trades ──
            const Padding(
              padding: EdgeInsets.only(left: 4, bottom: 8, top: 4),
              child: Text("TODAY'S TRADES",
                  style: TextStyle(color: kMuted, fontSize: 11, letterSpacing: 1.5)),
            ),
            if (_todayTrades.isEmpty)
              const Card(
                child: Padding(
                  padding: EdgeInsets.all(20),
                  child: Center(child: Text('No trades today', style: TextStyle(color: kMuted))),
                ),
              )
            else
              ..._todayTrades.map((t) => _TradeTile(trade: t as Map<String, dynamic>)),
            const SizedBox(height: 24),
          ],
        ),
      ),
    );
  }

  Widget _openPosition(Map<String, dynamic> st, double? pnl) {
    final pnlColor = pnl == null ? kMuted : (pnl >= 0 ? kGreen : kRed);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
              child: Text(st['symbol'] as String? ?? '—',
                  style: const TextStyle(color: kGold, fontWeight: FontWeight.w700, fontSize: 16)),
            ),
            Text(fmtUsd(pnl),
                style: TextStyle(color: pnlColor, fontWeight: FontWeight.w800, fontSize: 24)),
          ],
        ),
        const SizedBox(height: 4),
        const Divider(color: kBorder),
        _kv('Strike', '\$${fmtNum(st['strike'])}'),
        _kv('Lots', '${st['lots'] ?? '—'}'),
        _kv('Entry Mark', fmtUsd(st['entry_mark'], dp: 4)),
        _kv('Current Mark', fmtUsd(st['current_mark'], dp: 4)),
        _kv('Total Cost', fmtUsd(st['total_cost_usd'])),
        _kv('Settlement', (st['settlement'] as String? ?? '').replaceAll('T', ' ').replaceAll('Z', ' UTC')),
        const SizedBox(height: 14),
        SizedBox(
          width: double.infinity,
          child: OutlinedButton(
            style: OutlinedButton.styleFrom(
              foregroundColor: kRed,
              side: const BorderSide(color: kRed, width: 1.5),
              padding: const EdgeInsets.symmetric(vertical: 13),
            ),
            onPressed: _squareOff,
            child: const Text('⏹  SQUARE OFF POSITION',
                style: TextStyle(fontWeight: FontWeight.w700, letterSpacing: 1)),
          ),
        ),
      ],
    );
  }

  Widget _noPosition(Map<String, dynamic> st) {
    final closed = st['status'] == 'CLOSED';
    return Column(
      children: [
        Text(closed ? '✅' : '⏳', style: const TextStyle(fontSize: 34)),
        const SizedBox(height: 8),
        Text(
          closed ? 'Closed today  ·  P&L ${fmtUsd(st['pnl_usd'])}' : 'Waiting for entry window',
          style: TextStyle(
            color: closed
                ? (((st['pnl_usd'] as num?)?.toDouble() ?? 0) >= 0 ? kGreen : kRed)
                : kMuted,
            fontWeight: FontWeight.w600,
          ),
        ),
        const SizedBox(height: 6),
        Text(
          'Entry: ${st['entry_ist'] ?? '5:35 PM IST'}\nExit: ${st['exit_ist'] ?? '1:00 AM IST'}',
          textAlign: TextAlign.center,
          style: const TextStyle(color: kMuted, fontSize: 12, height: 1.5),
        ),
      ],
    );
  }

  Widget _kv(String k, String v) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(
          children: [
            Text(k, style: const TextStyle(color: kMuted, fontSize: 13)),
            const Spacer(),
            Text(v,
                style: const TextStyle(
                    color: kText, fontSize: 13, fontFeatures: [FontFeature.tabularFigures()])),
          ],
        ),
      );
}

class _StatusPill extends StatelessWidget {
  final String status;
  const _StatusPill({required this.status});

  @override
  Widget build(BuildContext context) {
    final (color, label) = switch (status) {
      'OPEN' => (kGreen, 'POSITION OPEN'),
      'CLOSED' => (kGold, 'CLOSED TODAY'),
      _ => (kMuted, 'AWAITING ENTRY'),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(20),
        color: color.withValues(alpha: 0.1),
        border: Border.all(color: color.withValues(alpha: 0.5)),
      ),
      child: Text(label,
          style: TextStyle(color: color, fontSize: 10, fontWeight: FontWeight.w700, letterSpacing: 1)),
    );
  }
}

class _TradeTile extends StatelessWidget {
  final Map<String, dynamic> trade;
  const _TradeTile({required this.trade});

  @override
  Widget build(BuildContext context) {
    final live = trade['_live'] == true;
    final pnl = ((live ? trade['live_pnl'] : trade['pnl_usd']) as num?)?.toDouble();
    final pnlColor = pnl == null ? kMuted : (pnl >= 0 ? kGreen : kRed);
    return Card(
      child: ListTile(
        title: Text(trade['symbol'] as String? ?? '—',
            style: TextStyle(
                color: live ? kGold : kText, fontWeight: FontWeight.w600, fontSize: 14)),
        subtitle: Text(
          '${trade['lots'] ?? ''} lots  ·  entry ${fmtUsd(trade['entry_mark'], dp: 4)}',
          style: const TextStyle(color: kMuted, fontSize: 12),
        ),
        trailing: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Text(fmtUsd(pnl),
                style: TextStyle(color: pnlColor, fontWeight: FontWeight.w700, fontSize: 15)),
            Text(live ? 'LIVE' : (pnl != null && pnl >= 0 ? 'WIN' : 'LOSS'),
                style: TextStyle(
                    color: live ? kGold : pnlColor, fontSize: 10, fontWeight: FontWeight.w700)),
          ],
        ),
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────
// Logs page
// ─────────────────────────────────────────────────────────────
class LogsPage extends StatefulWidget {
  const LogsPage({super.key});

  @override
  State<LogsPage> createState() => _LogsPageState();
}

class _LogsPageState extends State<LogsPage> {
  List<String> _lines = [];
  bool _loading = false;
  int _n = 100;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() => _loading = true);
    try {
      final d = await Api.getJson('/api/logs?n=$_n');
      if (!mounted) return;
      setState(() => _lines = (d['lines'] as List).cast<String>());
    } catch (e) {
      if (!mounted) return;
      setState(() => _lines = ['Error loading logs: $e']);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Color _lineColor(String l) {
    final u = l.toUpperCase();
    if (u.contains('ERROR')) return kRed;
    if (u.contains('WARN')) return const Color(0xFFFFA500);
    if (u.contains('ORDER') || u.contains('ENTRY') || u.contains('EXIT') || u.contains('TP HIT')) {
      return kGreen;
    }
    return kMuted;
  }

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
            child: Row(
              children: [
                const Text('BOT LOGS',
                    style: TextStyle(color: kMuted, fontSize: 12, letterSpacing: 1.5)),
                const Spacer(),
                DropdownButton<int>(
                  value: _n,
                  dropdownColor: kSurf,
                  underline: const SizedBox.shrink(),
                  style: const TextStyle(color: kText, fontSize: 12),
                  items: const [
                    DropdownMenuItem(value: 50, child: Text('50 lines')),
                    DropdownMenuItem(value: 100, child: Text('100 lines')),
                    DropdownMenuItem(value: 200, child: Text('200 lines')),
                    DropdownMenuItem(value: 500, child: Text('500 lines')),
                  ],
                  onChanged: (v) {
                    if (v != null) {
                      _n = v;
                      _load();
                    }
                  },
                ),
                IconButton(onPressed: _load, icon: const Icon(Icons.refresh, color: kGreen)),
              ],
            ),
          ),
          Expanded(
            child: _loading
                ? const Center(child: CircularProgressIndicator(color: kGreen))
                : RefreshIndicator(
                    color: kGreen,
                    onRefresh: _load,
                    child: ListView.builder(
                      reverse: true,
                      padding: const EdgeInsets.symmetric(horizontal: 12),
                      itemCount: _lines.length,
                      itemBuilder: (ctx, i) {
                        final line = _lines[_lines.length - 1 - i];
                        return Padding(
                          padding: const EdgeInsets.symmetric(vertical: 2),
                          child: Text(
                            line,
                            style: TextStyle(
                              color: _lineColor(line),
                              fontSize: 11,
                              fontFamily: 'monospace',
                            ),
                          ),
                        );
                      },
                    ),
                  ),
          ),
        ],
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────
// Settings page
// ─────────────────────────────────────────────────────────────
class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key});

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  late final TextEditingController _urlCtl;
  String? _testResult;
  bool _testing = false;

  @override
  void initState() {
    super.initState();
    _urlCtl = TextEditingController(text: Api.baseUrl);
  }

  @override
  void dispose() {
    _urlCtl.dispose();
    super.dispose();
  }

  Future<void> _saveAndTest() async {
    setState(() {
      _testing = true;
      _testResult = null;
    });
    await Api.saveBaseUrl(_urlCtl.text);
    try {
      final d = await Api.getJson('/api/status');
      setState(() => _testResult = '✅ Connected — bot status: ${d['status'] ?? 'unknown'}');
    } catch (e) {
      setState(() => _testResult = '❌ Cannot connect: $e');
    } finally {
      setState(() => _testing = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text('SETTINGS', style: TextStyle(color: kMuted, fontSize: 12, letterSpacing: 1.5)),
          const SizedBox(height: 16),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text('Bot Server URL',
                      style: TextStyle(color: kText, fontWeight: FontWeight.w600)),
                  const SizedBox(height: 4),
                  const Text(
                    'The PC running dashboard.py — must be reachable from this phone (same Wi-Fi).',
                    style: TextStyle(color: kMuted, fontSize: 12),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _urlCtl,
                    keyboardType: TextInputType.url,
                    style: const TextStyle(fontFamily: 'monospace'),
                    decoration: InputDecoration(
                      hintText: 'http://192.168.1.8:5001',
                      border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                    ),
                  ),
                  const SizedBox(height: 12),
                  SizedBox(
                    width: double.infinity,
                    child: FilledButton(
                      style: FilledButton.styleFrom(
                        backgroundColor: kGreen.withValues(alpha: 0.15),
                        foregroundColor: kGreen,
                      ),
                      onPressed: _testing ? null : _saveAndTest,
                      child: Text(_testing ? 'Testing…' : 'Save & Test Connection'),
                    ),
                  ),
                  if (_testResult != null) ...[
                    const SizedBox(height: 12),
                    Text(_testResult!,
                        style: TextStyle(
                            color: _testResult!.startsWith('✅') ? kGreen : kRed, fontSize: 13)),
                  ],
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),
          const Card(
            child: Padding(
              padding: EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('About', style: TextStyle(color: kText, fontWeight: FontWeight.w600)),
                  SizedBox(height: 8),
                  Text(
                    'Mathi-bot mobile — MV-BTC daily straddle bot on Delta Exchange India.\n\n'
                    'Entry 5:35 PM IST · Exit 1:00 AM IST\n'
                    'This app is a remote control for the bot running on your PC.',
                    style: TextStyle(color: kMuted, fontSize: 12, height: 1.5),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}
