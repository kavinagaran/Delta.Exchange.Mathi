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

class SlotMeta {
  final String key, name, icon, entryLabel;
  const SlotMeta(this.key, this.name, this.icon, this.entryLabel);
}

const kSlots = [
  SlotMeta('morning', 'Morning', '🌅', '5:45 AM IST'),
  SlotMeta('evening', 'Evening', '🌇', '5:35 PM IST'),
];

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
// Dashboard page — morning + evening slots
// ─────────────────────────────────────────────────────────────
class DashboardPage extends StatefulWidget {
  const DashboardPage({super.key});

  @override
  State<DashboardPage> createState() => _DashboardPageState();
}

class _DashboardPageState extends State<DashboardPage> {
  Map<String, dynamic> _evening = {};
  Map<String, dynamic> _morning = {};
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

  Map<String, dynamic> _slotState(String slot) =>
      slot == 'morning' ? _morning : _evening;

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
        _evening = st;
        _morning = (st['morning'] as Map<String, dynamic>?) ?? {};
        _todayTrades = results[1] as List<dynamic>;
        _tp = results[2] as Map<String, dynamic>;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = 'Cannot reach bot server:\n${Api.baseUrl}\n\n$e');
    }
  }

  Future<void> _squareOff(SlotMeta slot) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: kSurf,
        title: Text('Square Off ${slot.name}?'),
        content: Text('Close the entire ${slot.name.toLowerCase()} position now at market price?'),
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
      final d = await Api.postJson('/api/square-off?slot=${slot.key}');
      if (!mounted) return;
      final msg = d['ok'] == true
          ? '${slot.name} closed  P&L: ${fmtUsd(d['pnl'])}'
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

  Future<void> _toggleTp(SlotMeta slot) async {
    final cfg = (_tp[slot.key] as Map<String, dynamic>?) ?? {};
    final running = cfg['running'] == true;
    try {
      final d = await Api.postJson(
          '/api/tp-monitor/${running ? 'stop' : 'start'}?slot=${slot.key}');
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(d['ok'] == true
            ? '${slot.name} TP monitor ${running ? 'stopped' : 'started'}'
            : 'Error: ${d['error']}'),
      ));
      _refresh();
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Error: $e')));
    }
  }

  Future<void> _editTpConfig(SlotMeta slot) async {
    final cfg = (_tp[slot.key] as Map<String, dynamic>?) ?? {};
    final targetCtl = TextEditingController(text: fmtNum(cfg['target_pnl'], dp: 0));
    final pollCtl = TextEditingController(text: fmtNum(cfg['poll_secs'], dp: 0));
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: kSurf,
        title: Text('${slot.icon} ${slot.name} TP Config'),
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
    final body = slot.key == 'morning'
        ? {'TP_TARGET_PNL_MORNING': targetCtl.text, 'TP_POLL_SECS_MORNING': pollCtl.text}
        : {'TP_TARGET_PNL': targetCtl.text, 'TP_POLL_SECS': pollCtl.text};
    try {
      await Api.postJson('/api/config', body);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('${slot.name} TP config saved')));
      _refresh();
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Error: $e')));
    }
  }

  @override
  Widget build(BuildContext context) {
    final btc = (_evening['btc_futures_price'] as num?)?.toDouble();
    final openSlots = kSlots.where((s) => _slotState(s.key)['status'] == 'OPEN').toList();
    final totalPnl = openSlots.fold<double>(
        0, (a, s) => a + (((_slotState(s.key)['live_pnl']) as num?)?.toDouble() ?? 0));

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
                _StatusPill(
                  openCount: openSlots.length,
                  anyClosed: kSlots.any((s) => _slotState(s.key)['status'] == 'CLOSED'),
                ),
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
            if (openSlots.isNotEmpty) ...[
              const SizedBox(height: 8),
              Center(
                child: Text(
                  'Combined live P&L: ${fmtUsd(totalPnl)}',
                  style: TextStyle(
                    color: totalPnl >= 0 ? kGreen : kRed,
                    fontWeight: FontWeight.w700,
                    fontSize: 13,
                  ),
                ),
              ),
            ],
            const SizedBox(height: 14),

            if (_error != null)
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Text(_error!, style: const TextStyle(color: kRed)),
                ),
              ),

            // ── Slot cards ──
            for (final slot in kSlots) ...[
              _slotCard(slot),
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

  Widget _slotCard(SlotMeta slot) {
    final st = _slotState(slot.key);
    final open = st['status'] == 'OPEN';
    final closed = st['status'] == 'CLOSED';
    final tpCfg = (_tp[slot.key] as Map<String, dynamic>?) ?? {};

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Text('${slot.icon} ${slot.name.toUpperCase()} TRADE',
                    style: const TextStyle(color: kMuted, fontSize: 11, letterSpacing: 1.2, fontWeight: FontWeight.w700)),
                const Spacer(),
                Text(slot.entryLabel, style: const TextStyle(color: kGold, fontSize: 10)),
              ],
            ),
            const SizedBox(height: 10),
            if (open)
              _openBody(slot, st, tpCfg)
            else if (closed)
              _closedBody(st)
            else
              _idleBody(slot),
          ],
        ),
      ),
    );
  }

  Widget _openBody(SlotMeta slot, Map<String, dynamic> st, Map<String, dynamic> tpCfg) {
    final pnl = (st['live_pnl'] as num?)?.toDouble();
    final pnlColor = pnl == null ? kMuted : (pnl >= 0 ? kGreen : kRed);
    final tpRunning = tpCfg['running'] == true;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
              child: Text(st['symbol'] as String? ?? '—',
                  style: const TextStyle(color: kGold, fontWeight: FontWeight.w700, fontSize: 15)),
            ),
            Text(fmtUsd(pnl),
                style: TextStyle(color: pnlColor, fontWeight: FontWeight.w800, fontSize: 22)),
          ],
        ),
        const Divider(color: kBorder),
        _kv('Strike', '\$${fmtNum(st['strike'])}'),
        _kv('Lots', '${st['lots'] ?? '—'}'),
        _kv('Entry Mark', fmtUsd(st['entry_mark'], dp: 4)),
        _kv('Current Mark', fmtUsd(st['current_mark'], dp: 4)),
        _kv('Total Cost', fmtUsd(st['total_cost_usd'])),
        _kv('Settlement', (st['settlement'] as String? ?? '').replaceAll('T', ' ').replaceAll('Z', ' UTC')),
        const SizedBox(height: 12),
        Row(
          children: [
            Expanded(
              child: OutlinedButton(
                style: OutlinedButton.styleFrom(
                  foregroundColor: kRed,
                  side: const BorderSide(color: kRed, width: 1.5),
                  padding: const EdgeInsets.symmetric(vertical: 11),
                ),
                onPressed: () => _squareOff(slot),
                child: const Text('⏹ SQUARE OFF', style: TextStyle(fontWeight: FontWeight.w700)),
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),
        Container(
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            border: Border.all(color: kBorder),
            borderRadius: BorderRadius.circular(8),
          ),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text('TP MONITOR',
                        style: TextStyle(color: kMuted, fontSize: 9, letterSpacing: 1)),
                    const SizedBox(height: 2),
                    Text(
                      tpRunning ? '● Running' : '○ Stopped',
                      style: TextStyle(
                        color: tpRunning ? kGreen : kMuted,
                        fontWeight: FontWeight.w700,
                        fontSize: 13,
                      ),
                    ),
                    Text(
                      'Target ${fmtUsd(tpCfg['target_pnl'], dp: 0)} · ${fmtNum(tpCfg['poll_secs'])}s',
                      style: const TextStyle(color: kMuted, fontSize: 11),
                    ),
                  ],
                ),
              ),
              IconButton(
                onPressed: () => _editTpConfig(slot),
                icon: const Icon(Icons.tune, color: kGold, size: 20),
              ),
              FilledButton(
                style: FilledButton.styleFrom(
                  backgroundColor: tpRunning ? kRed.withValues(alpha: 0.15) : kGreen.withValues(alpha: 0.15),
                  foregroundColor: tpRunning ? kRed : kGreen,
                  padding: const EdgeInsets.symmetric(horizontal: 14),
                ),
                onPressed: () => _toggleTp(slot),
                child: Text(tpRunning ? 'STOP' : 'START', style: const TextStyle(fontSize: 12)),
              ),
            ],
          ),
        ),
      ],
    );
  }

  Widget _closedBody(Map<String, dynamic> st) {
    final pnl = (st['pnl_usd'] as num?)?.toDouble() ?? 0;
    return Row(
      children: [
        const Text('✅', style: TextStyle(fontSize: 22)),
        const SizedBox(width: 10),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('Closed ${st['exit_time_utc'] ?? ''} UTC · ${st['symbol'] ?? ''}',
                  style: const TextStyle(color: kMuted, fontSize: 12)),
              Text('P&L ${fmtUsd(pnl)}',
                  style: TextStyle(
                      color: pnl >= 0 ? kGreen : kRed, fontWeight: FontWeight.w700, fontSize: 16)),
            ],
          ),
        ),
      ],
    );
  }

  Widget _idleBody(SlotMeta slot) {
    return Row(
      children: [
        const Text('⏳', style: TextStyle(fontSize: 22)),
        const SizedBox(width: 10),
        Text('Waiting for entry — ${slot.entryLabel}',
            style: const TextStyle(color: kMuted, fontSize: 13)),
      ],
    );
  }

  Widget _kv(String k, String v) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(
          children: [
            Text(k, style: const TextStyle(color: kMuted, fontSize: 12.5)),
            const Spacer(),
            Text(v,
                style: const TextStyle(
                    color: kText, fontSize: 12.5, fontFeatures: [FontFeature.tabularFigures()])),
          ],
        ),
      );
}

class _StatusPill extends StatelessWidget {
  final int openCount;
  final bool anyClosed;
  const _StatusPill({required this.openCount, required this.anyClosed});

  @override
  Widget build(BuildContext context) {
    final (color, label) = openCount == 2
        ? (kGreen, 'BOTH OPEN')
        : openCount == 1
            ? (kGreen, 'TRADE OPEN')
            : anyClosed
                ? (kGold, 'CLOSED TODAY')
                : (kMuted, 'AWAITING');
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
    final slotIcon = trade['slot'] == 'morning' ? '🌅 ' : (trade['slot'] == 'evening' ? '🌇 ' : '');
    final pnl = ((live ? trade['live_pnl'] : trade['pnl_usd']) as num?)?.toDouble();
    final pnlColor = pnl == null ? kMuted : (pnl >= 0 ? kGreen : kRed);
    return Card(
      child: ListTile(
        title: Text('$slotIcon${trade['symbol'] ?? '—'}',
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
  Map<String, dynamic> _config = {};
  bool _configLoaded = false;

  @override
  void initState() {
    super.initState();
    _urlCtl = TextEditingController(text: Api.baseUrl);
    _loadConfig();
  }

  @override
  void dispose() {
    _urlCtl.dispose();
    super.dispose();
  }

  Future<void> _loadConfig() async {
    try {
      final d = await Api.getJson('/api/config');
      if (!mounted) return;
      setState(() {
        _config = d as Map<String, dynamic>;
        _configLoaded = true;
      });
    } catch (_) {}
  }

  Future<void> _saveAndTest() async {
    setState(() {
      _testing = true;
      _testResult = null;
    });
    await Api.saveBaseUrl(_urlCtl.text);
    try {
      final d = await Api.getJson('/api/status');
      setState(() => _testResult = '✅ Connected — evening: ${d['status'] ?? '?'}, morning: ${d['morning']?['status'] ?? '—'}');
      _loadConfig();
    } catch (e) {
      setState(() => _testResult = '❌ Cannot connect: $e');
    } finally {
      setState(() => _testing = false);
    }
  }

  // IST <-> UTC minute-of-day conversions (IST = UTC + 5:30)
  static (int, int) _utcToIst(int h, int m) {
    final t = (h * 60 + m + 330) % 1440;
    return (t ~/ 60, t % 60);
  }

  static (int, int) _istToUtc(int h, int m) {
    final t = ((h * 60 + m - 330) % 1440 + 1440) % 1440;
    return (t ~/ 60, t % 60);
  }

  Future<void> _editMorningConfig() async {
    final enabled = (_config['MORNING_ENABLED'] ?? 'true').toString().toLowerCase() != 'false';
    final lotsCtl = TextEditingController(text: (_config['MORNING_LOTS'] ?? '2000').toString());
    final (exH, exM) = _utcToIst(
      int.tryParse((_config['MORNING_EXIT_H_UTC'] ?? '11').toString()) ?? 11,
      int.tryParse((_config['MORNING_EXIT_M_UTC'] ?? '30').toString()) ?? 30,
    );
    final exitHCtl = TextEditingController(text: exH.toString());
    final exitMCtl = TextEditingController(text: exM.toString());
    bool en = enabled;
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setSt) => AlertDialog(
          backgroundColor: kSurf,
          title: const Text('🌅 Morning Trade Config'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              SwitchListTile(
                title: const Text('Enabled', style: TextStyle(fontSize: 14)),
                value: en,
                activeColor: kGreen,
                contentPadding: EdgeInsets.zero,
                onChanged: (v) => setSt(() => en = v),
              ),
              TextField(
                controller: lotsCtl,
                keyboardType: TextInputType.number,
                decoration: const InputDecoration(labelText: 'Lots'),
              ),
              Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: exitHCtl,
                      keyboardType: TextInputType.number,
                      decoration: const InputDecoration(labelText: 'Exit Hour (IST)'),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: TextField(
                      controller: exitMCtl,
                      keyboardType: TextInputType.number,
                      decoration: const InputDecoration(labelText: 'Exit Min (IST)'),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              const Text('Entry: 5:45 AM IST · settles 5:30 PM if not exited',
                  style: TextStyle(color: kMuted, fontSize: 11)),
            ],
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
            TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Save', style: TextStyle(color: kGold))),
          ],
        ),
      ),
    );
    if (ok != true) return;
    final ih = int.tryParse(exitHCtl.text) ?? 17;
    final im = int.tryParse(exitMCtl.text) ?? 0;
    final (uh, um) = _istToUtc(ih, im);
    try {
      await Api.postJson('/api/config', {
        'MORNING_ENABLED': en ? 'true' : 'false',
        'MORNING_LOTS': lotsCtl.text,
        'MORNING_EXIT_H_UTC': uh.toString(),
        'MORNING_EXIT_M_UTC': um.toString(),
      });
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Morning config saved — restart bot to apply')));
      _loadConfig();
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Error: $e')));
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
          Card(
            child: ListTile(
              leading: const Text('🌅', style: TextStyle(fontSize: 22)),
              title: const Text('Morning Trade', style: TextStyle(color: kText, fontSize: 14)),
              subtitle: Text(
                _configLoaded
                    ? '${(_config['MORNING_ENABLED'] ?? 'true').toString().toLowerCase() != 'false' ? 'Enabled' : 'Disabled'} · ${_config['MORNING_LOTS'] ?? 2000} lots · 5:45 AM IST'
                    : 'Loading…',
                style: const TextStyle(color: kMuted, fontSize: 12),
              ),
              trailing: const Icon(Icons.chevron_right, color: kMuted),
              onTap: _editMorningConfig,
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
                    '🌅 Morning trade: 5:45 AM IST (settles 5:30 PM)\n'
                    '🌇 Evening trade: 5:35 PM IST (exits 2:30 AM)\n\n'
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
