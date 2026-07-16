import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

// ─────────────────────────────────────────────────────────────
// Design tokens — shared with static/css/app.css in the web dashboard.
// ─────────────────────────────────────────────────────────────
const kBg = Color(0xFFF2F4F8);
const kSurf = Color(0xFFFFFFFF);
const kBorder = Color(0xFFE4E8F0);
const kGreen = Color(0xFF08875D);
const kRed = Color(0xFFD92D20);
const kGold = Color(
  0xFF2563EB,
); // Legacy name retained: this is web accent blue.
const kMuted = Color(0xFF68758A);
const kText = Color(0xFF1A2333);
const kSidebar = Color(0xFF161D29);
const kSidebarMuted = Color(0xFF8F9CB0);
const kWarn = Color(0xFFB45309);

void main() {
  runApp(const MathiBotApp());
}

class MathiBotApp extends StatelessWidget {
  const MathiBotApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Nithi-bot',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        brightness: Brightness.light,
        fontFamily: 'Roboto',
        scaffoldBackgroundColor: kBg,
        colorScheme: const ColorScheme.light(
          primary: kGold,
          surface: kSurf,
          error: kRed,
          onPrimary: Colors.white,
          onSurface: kText,
        ),
        appBarTheme: const AppBarTheme(
          backgroundColor: kSurf,
          foregroundColor: kText,
          elevation: 0,
          centerTitle: false,
          surfaceTintColor: Colors.transparent,
          shape: Border(bottom: BorderSide(color: kBorder)),
          titleTextStyle: TextStyle(
            color: kText,
            fontSize: 17,
            fontWeight: FontWeight.w700,
            letterSpacing: -0.15,
          ),
        ),
        cardTheme: const CardThemeData(
          color: kSurf,
          elevation: 0,
          surfaceTintColor: Colors.transparent,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(10)),
            side: BorderSide(color: kBorder),
          ),
        ),
        dividerTheme: const DividerThemeData(color: kBorder, thickness: 1),
        inputDecorationTheme: InputDecorationTheme(
          filled: true,
          fillColor: kSurf,
          labelStyle: const TextStyle(color: kMuted, fontSize: 13),
          hintStyle: const TextStyle(color: kMuted, fontSize: 13),
          contentPadding: const EdgeInsets.symmetric(
            horizontal: 13,
            vertical: 13,
          ),
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(8),
            borderSide: const BorderSide(color: kBorder),
          ),
          enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(8),
            borderSide: const BorderSide(color: kBorder),
          ),
          focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(8),
            borderSide: const BorderSide(color: kGold, width: 1.5),
          ),
        ),
        filledButtonTheme: FilledButtonThemeData(
          style: FilledButton.styleFrom(
            backgroundColor: kGold,
            foregroundColor: Colors.white,
            textStyle: const TextStyle(fontWeight: FontWeight.w700),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(8),
            ),
          ),
        ),
        navigationBarTheme: const NavigationBarThemeData(
          backgroundColor: kSurf,
          indicatorColor: Color(0xFFEEF3FE),
          labelTextStyle: WidgetStatePropertyAll(
            TextStyle(
              fontSize: 10.5,
              fontWeight: FontWeight.w600,
              color: kMuted,
            ),
          ),
          iconTheme: WidgetStatePropertyAll(
            IconThemeData(color: kMuted, size: 21),
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
  static String baseUrl = 'http://13.207.78.56:5001';
  static String user = 'mathi';
  static String pass = '';
  static String displayName = '';

  static Map<String, String> get _headers => {
    'Content-Type': 'application/json',
    if (pass.isNotEmpty)
      'Authorization': 'Basic ${base64Encode(utf8.encode('$user:$pass'))}',
  };

  static Future<void> loadBaseUrl() async {
    final prefs = await SharedPreferences.getInstance();
    baseUrl = prefs.getString('server_url') ?? baseUrl;
    user = prefs.getString('server_user') ?? user;
    pass = prefs.getString('server_pass') ?? pass;
  }

  static Future<void> saveBaseUrl(String url, String u, String p) async {
    baseUrl = url.trim().replaceAll(RegExp(r'/+$'), '');
    user = u.trim();
    pass = p;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('server_url', baseUrl);
    await prefs.setString('server_user', user);
    await prefs.setString('server_pass', pass);
  }

  static Future<dynamic> getJson(String path) async {
    final r = await http
        .get(Uri.parse('$baseUrl$path'), headers: _headers)
        .timeout(const Duration(seconds: 8));
    if (r.statusCode == 401) {
      throw Exception('Login failed — check username/password in Settings');
    }
    return jsonDecode(r.body);
  }

  static Future<dynamic> postJson(
    String path, [
    Map<String, dynamic>? body,
  ]) async {
    final r = await http
        .post(
          Uri.parse('$baseUrl$path'),
          headers: _headers,
          body: body == null ? null : jsonEncode(body),
        )
        .timeout(const Duration(seconds: 20));
    if (r.statusCode == 401) {
      throw Exception('Login failed — check username/password in Settings');
    }
    return jsonDecode(r.body);
  }

  static Future<dynamic> deleteJson(String path) async {
    final r = await http
        .delete(Uri.parse('$baseUrl$path'), headers: _headers)
        .timeout(const Duration(seconds: 15));
    if (r.statusCode == 401) {
      throw Exception('Login failed — check username/password in Settings');
    }
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

/// Same IST boundary the exchange-sync classifier uses: before 11:00 IST
/// belongs to the morning slot's trading window, after to the evening's.
String manualSlotNow() {
  final now = DateTime.now().toUtc();
  final istMin = (now.hour * 60 + now.minute + 330) % 1440;
  return istMin < 660 ? 'morning' : 'evening';
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
  bool _authed = false;

  @override
  void initState() {
    super.initState();
    _init();
  }

  Future<void> _init() async {
    await Api.loadBaseUrl();
    bool ok = false;
    if (Api.pass.isNotEmpty) {
      try {
        final me = await Api.getJson('/api/me') as Map<String, dynamic>;
        Api.displayName = (me['display_name'] ?? Api.user).toString();
        ok = true;
      } catch (_) {}
    }
    if (!mounted) return;
    setState(() {
      _ready = true;
      _authed = ok;
    });
  }

  void _signOut() {
    Api.saveBaseUrl(Api.baseUrl, Api.user, '');
    Api.displayName = '';
    setState(() {
      _authed = false;
      _tab = 0;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (!_ready) {
      return const Scaffold(
        body: Center(child: CircularProgressIndicator(color: kGold)),
      );
    }
    if (!_authed) {
      return LoginScreen(onSuccess: () => setState(() => _authed = true));
    }
    const labels = [
      'Overview',
      'Trades & P&L',
      'Positions',
      'Bot Config',
      'API Accounts',
      'Settings',
    ];
    const icons = [
      Icons.dashboard_outlined,
      Icons.show_chart_outlined,
      Icons.view_list_outlined,
      Icons.tune_outlined,
      Icons.manage_accounts_outlined,
      Icons.settings_outlined,
    ];
    final pages = <Widget>[
      const DashboardPage(),
      const TradesPage(),
      const PositionsPage(),
      const ConfigsPage(),
      const AccountsPage(),
      SettingsPage(onSignOut: _signOut),
    ];

    return LayoutBuilder(
      builder: (context, bounds) {
        final wide = bounds.maxWidth >= 840;
        final body = IndexedStack(index: _tab, children: pages);
        if (!wide) {
          return Scaffold(
            appBar: AppBar(
              title: Text(labels[_tab]),
              leadingWidth: 48,
              leading: Padding(
                padding: const EdgeInsets.all(10),
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(7),
                  child: Image.asset('assets/logo.png'),
                ),
              ),
            ),
            body: body,
            bottomNavigationBar: NavigationBar(
              height: 68,
              selectedIndex: _tab,
              onDestinationSelected: (i) => setState(() => _tab = i),
              destinations: List.generate(
                labels.length,
                (i) => NavigationDestination(
                  icon: Icon(icons[i]),
                  selectedIcon: Icon(icons[i], color: kGold),
                  label: i == 1
                      ? 'Trades'
                      : i == 3
                      ? 'Config'
                      : i == 4
                      ? 'Accounts'
                      : labels[i],
                ),
              ),
            ),
          );
        }

        return Scaffold(
          body: Row(
            children: [
              Container(
                width: 224,
                color: kSidebar,
                child: SafeArea(
                  child: Column(
                    children: [
                      Padding(
                        padding: const EdgeInsets.fromLTRB(18, 20, 18, 16),
                        child: Row(
                          children: [
                            ClipRRect(
                              borderRadius: BorderRadius.circular(10),
                              child: Image.asset(
                                'assets/logo.png',
                                width: 38,
                                height: 38,
                              ),
                            ),
                            const SizedBox(width: 10),
                            const Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(
                                    'NITHI-BOT',
                                    style: TextStyle(
                                      color: Colors.white,
                                      fontWeight: FontWeight.w700,
                                      fontSize: 15,
                                      letterSpacing: .9,
                                    ),
                                  ),
                                  Text(
                                    'BTC Trade Made Bit Easy',
                                    maxLines: 1,
                                    style: TextStyle(
                                      color: kSidebarMuted,
                                      fontSize: 9.5,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          ],
                        ),
                      ),
                      const Divider(color: Color(0xFF232C3D), height: 1),
                      const SizedBox(height: 10),
                      Expanded(
                        child: ListView.builder(
                          padding: const EdgeInsets.symmetric(horizontal: 10),
                          itemCount: labels.length,
                          itemBuilder: (_, i) => Padding(
                            padding: const EdgeInsets.only(bottom: 2),
                            child: ListTile(
                              dense: true,
                              selected: _tab == i,
                              selectedTileColor: kGold.withValues(alpha: .22),
                              shape: RoundedRectangleBorder(
                                borderRadius: BorderRadius.circular(8),
                              ),
                              leading: Icon(
                                icons[i],
                                size: 19,
                                color: _tab == i ? Colors.white : kSidebarMuted,
                              ),
                              title: Text(
                                labels[i],
                                style: TextStyle(
                                  color: _tab == i
                                      ? Colors.white
                                      : kSidebarMuted,
                                  fontSize: 13.5,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                              onTap: () => setState(() => _tab = i),
                            ),
                          ),
                        ),
                      ),
                      const Divider(color: Color(0xFF232C3D), height: 1),
                      Padding(
                        padding: const EdgeInsets.all(16),
                        child: Row(
                          children: [
                            const CircleAvatar(
                              radius: 16,
                              backgroundColor: Color(0xFF232C3D),
                              child: Icon(
                                Icons.person_outline,
                                size: 17,
                                color: Colors.white,
                              ),
                            ),
                            const SizedBox(width: 9),
                            Expanded(
                              child: Text(
                                Api.displayName.isEmpty
                                    ? Api.user
                                    : Api.displayName,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  color: Colors.white,
                                  fontWeight: FontWeight.w600,
                                  fontSize: 12,
                                ),
                              ),
                            ),
                            IconButton(
                              onPressed: _signOut,
                              tooltip: 'Sign out',
                              icon: const Icon(
                                Icons.logout,
                                size: 18,
                                color: kSidebarMuted,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              Expanded(
                child: Column(
                  children: [
                    Container(
                      height: 64,
                      padding: const EdgeInsets.symmetric(horizontal: 26),
                      decoration: const BoxDecoration(
                        color: kSurf,
                        border: Border(bottom: BorderSide(color: kBorder)),
                      ),
                      alignment: Alignment.centerLeft,
                      child: Text(
                        labels[_tab],
                        style: const TextStyle(
                          color: kText,
                          fontSize: 17,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                    Expanded(child: body),
                  ],
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}

// ─────────────────────────────────────────────────────────────
// Login screen — sign in as any configured account
// ─────────────────────────────────────────────────────────────
class LoginScreen extends StatefulWidget {
  final VoidCallback onSuccess;
  const LoginScreen({super.key, required this.onSuccess});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  late final TextEditingController _urlCtl = TextEditingController(
    text: Api.baseUrl,
  );
  late final TextEditingController _userCtl = TextEditingController(
    text: Api.user,
  );
  final TextEditingController _passCtl = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _urlCtl.dispose();
    _userCtl.dispose();
    _passCtl.dispose();
    super.dispose();
  }

  Future<void> _signIn() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    await Api.saveBaseUrl(_urlCtl.text, _userCtl.text, _passCtl.text);
    try {
      final me = await Api.getJson('/api/me') as Map<String, dynamic>;
      Api.displayName = (me['display_name'] ?? Api.user).toString();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Welcome back, ${Api.displayName}!')),
      );
      widget.onSuccess();
    } catch (e) {
      await Api.saveBaseUrl(_urlCtl.text, _userCtl.text, '');
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = e.toString().contains('Login failed')
            ? 'Wrong username or password'
            : 'Cannot reach server: $e';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(28),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Center(
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(20),
                    child: Image.asset(
                      'assets/logo.png',
                      width: 88,
                      height: 88,
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                const Center(
                  child: Text(
                    'NITHI-BOT',
                    style: TextStyle(
                      color: kGold,
                      fontSize: 26,
                      fontWeight: FontWeight.w800,
                      letterSpacing: 3,
                    ),
                  ),
                ),
                const SizedBox(height: 4),
                const Center(
                  child: Text(
                    'BTC Trade Made Bit Easy',
                    style: TextStyle(color: kMuted, fontSize: 12),
                  ),
                ),
                const SizedBox(height: 30),
                if (_error != null) ...[
                  Container(
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: kRed.withValues(alpha: 0.12),
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: Text(
                      _error!,
                      style: const TextStyle(color: kRed, fontSize: 13),
                    ),
                  ),
                  const SizedBox(height: 14),
                ],
                TextField(
                  controller: _userCtl,
                  autofillHints: const [AutofillHints.username],
                  decoration: InputDecoration(
                    labelText: 'Username',
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(8),
                    ),
                  ),
                ),
                const SizedBox(height: 14),
                TextField(
                  controller: _passCtl,
                  obscureText: true,
                  autofillHints: const [AutofillHints.password],
                  onSubmitted: (_) => _busy ? null : _signIn(),
                  decoration: InputDecoration(
                    labelText: 'Password',
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(8),
                    ),
                  ),
                ),
                const SizedBox(height: 14),
                ExpansionTile(
                  tilePadding: EdgeInsets.zero,
                  title: const Text(
                    'Server',
                    style: TextStyle(color: kMuted, fontSize: 13),
                  ),
                  children: [
                    TextField(
                      controller: _urlCtl,
                      keyboardType: TextInputType.url,
                      style: const TextStyle(
                        fontFamily: 'monospace',
                        fontSize: 13,
                      ),
                      decoration: InputDecoration(
                        labelText: 'Server URL',
                        border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(8),
                        ),
                      ),
                    ),
                    const SizedBox(height: 8),
                  ],
                ),
                const SizedBox(height: 18),
                FilledButton(
                  style: FilledButton.styleFrom(
                    padding: const EdgeInsets.symmetric(vertical: 15),
                  ),
                  onPressed: _busy ? null : _signIn,
                  child: Text(
                    _busy ? 'Signing in…' : 'SIGN IN',
                    style: const TextStyle(
                      fontWeight: FontWeight.w700,
                      letterSpacing: 1,
                    ),
                  ),
                ),
                const SizedBox(height: 10),
                const Center(
                  child: Text(
                    'Accounts are managed on the web dashboard → API Accounts',
                    style: TextStyle(color: kMuted, fontSize: 11),
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
  Map<String, dynamic> _wallet = {};
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
        Api.getJson('/api/wallet').catchError((_) => <String, dynamic>{}),
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
        _wallet = (results[3] as Map<String, dynamic>?) ?? {};
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = 'Cannot reach bot server:\n${Api.baseUrl}\n\n$e');
    }
  }

  double _intrinsic(String symbol, double strike, double s) {
    // MV = move option (straddle): pays |move from strike|. C/P = vanilla.
    if (symbol.startsWith('MV-')) return (s - strike).abs();
    if (symbol.startsWith('P-')) {
      return (strike - s).clamp(0, double.infinity).toDouble();
    }
    return (s - strike).clamp(0, double.infinity).toDouble();
  }

  void _showPayoff(Map<String, dynamic> st) {
    final symbol = (st['symbol'] ?? '').toString();
    final strike = (st['strike'] as num?)?.toDouble() ?? 0;
    final entry = (st['entry_mark'] as num?)?.toDouble() ?? 0;
    final lots = (st['lots'] as num?)?.toDouble() ?? 0;
    final cv = (st['contract_value'] as num?)?.toDouble() ?? 0.001;
    final sign = st['side'] == 'short' ? -1.0 : 1.0;
    final btc = (_evening['btc_futures_price'] as num?)?.toDouble() ?? strike;
    if (strike <= 0 || entry <= 0) return;

    final lo = (strike < btc ? strike : btc) * 0.955;
    final hi = (strike > btc ? strike : btc) * 1.045;
    const n = 120;
    final xs = <double>[], ys = <double>[];
    for (var i = 0; i <= n; i++) {
      final s = lo + (hi - lo) * i / n;
      xs.add(s);
      ys.add((_intrinsic(symbol, strike, s) - entry) * cv * lots * sign);
    }
    final pnlNow = (_intrinsic(symbol, strike, btc) - entry) * cv * lots * sign;
    final bes = symbol.startsWith('MV-')
        ? [strike - entry, strike + entry]
        : symbol.startsWith('P-')
        ? [strike - entry]
        : [strike + entry];

    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: kSurf,
        insetPadding: const EdgeInsets.symmetric(horizontal: 14),
        title: Text(
          'Payoff — $symbol',
          style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w700),
        ),
        content: SizedBox(
          width: double.maxFinite,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              SizedBox(
                height: 210,
                width: double.maxFinite,
                child: CustomPaint(
                  painter: PayoffPainter(
                    xs: xs,
                    ys: ys,
                    spot: btc,
                    spotPnl: pnlNow,
                  ),
                ),
              ),
              const SizedBox(height: 12),
              Text(
                '${st['side'] == 'short' ? 'SHORT' : 'LONG'} ${fmtNum(lots)} lots'
                ' · Strike \$${fmtNum(strike)} · Entry ${fmtUsd(entry, dp: 2)}\n'
                'Breakeven ${bes.map((b) => '\$${fmtNum(b)}').join(' / ')}'
                ' · BTC now \$${fmtNum(btc)}\n'
                'At current BTC: ${fmtUsd(pnlNow)}',
                style: const TextStyle(
                  color: kMuted,
                  fontSize: 12,
                  height: 1.6,
                ),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('Close'),
          ),
        ],
      ),
    );
  }

  Future<void> _squareOff(SlotMeta slot) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: kSurf,
        title: Text('Square Off ${slot.name}?'),
        content: Text(
          'Close the entire ${slot.name.toLowerCase()} position now at market price?',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text(
              'SQUARE OFF',
              style: TextStyle(color: kRed, fontWeight: FontWeight.bold),
            ),
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
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(msg),
          backgroundColor: d['ok'] == true ? kGreen : kRed,
        ),
      );
      _refresh();
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text('Error: $e')));
    }
  }

  Future<void> _manualEntry(SlotMeta slot, String side) async {
    final isBuy = side == 'buy';
    Map<String, dynamic> p;
    try {
      p =
          await Api.getJson('/api/manual-entry/preview?slot=${slot.key}')
              as Map<String, dynamic>;
      if (p['ok'] != true) throw Exception(p['error'] ?? 'preview failed');
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Cannot fetch straddle preview: $e')),
      );
      return;
    }
    if (!mounted) return;
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: kSurf,
        title: Text(isBuy ? '▲ Buy Straddle?' : '▼ Sell Straddle?'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              isBuy
                  ? 'BUY (long) at market — ${slot.name} slot'
                  : 'SELL-TO-OPEN (short, collect premium) at market — ${slot.name} slot',
            ),
            const SizedBox(height: 12),
            _previewRow('Contract', '${p['symbol']}'),
            _previewRow(
              'Strike',
              '\$${(p['strike'] as num).toStringAsFixed(0)}',
            ),
            _previewRow(
              'Mark',
              '\$${(p['mark'] as num).toStringAsFixed(2)} / BTC',
            ),
            _previewRow('Lots', '${p['lots']}'),
            _previewRow(
              'Value',
              '~\$${(p['est_value'] as num).toStringAsFixed(0)}',
            ),
            if (p['dry_run'] == true) ...[
              const SizedBox(height: 10),
              const Text(
                '⚠ Mode is DRY RUN — this will be SIMULATED, no real order will be placed.',
                style: TextStyle(
                  color: kGold,
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(
              isBuy ? 'BUY' : 'SELL',
              style: TextStyle(
                color: isBuy ? kGreen : kRed,
                fontWeight: FontWeight.bold,
              ),
            ),
          ),
        ],
      ),
    );
    if (ok != true) return;
    try {
      final d = await Api.postJson('/api/manual-entry?slot=${slot.key}', {
        'side': side,
      });
      if (!mounted) return;
      final simTag = d['dry_run'] == true ? ' (SIMULATED)' : '';
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            d['ok'] == true
                ? '${side.toUpperCase()} filled$simTag: ${d['lots']} lots ${d['symbol']} @ \$${(d['fill'] as num).toStringAsFixed(2)}'
                : 'Order failed: ${d['error']}',
          ),
          backgroundColor: d['ok'] == true ? kGreen : kRed,
        ),
      );
      _refresh();
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text('Error: $e')));
    }
  }

  Widget _previewRow(String k, String v) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 2),
    child: Row(
      children: [
        SizedBox(
          width: 80,
          child: Text(k, style: const TextStyle(color: kMuted, fontSize: 12.5)),
        ),
        Expanded(
          child: Text(
            v,
            style: const TextStyle(
              color: kText,
              fontSize: 13,
              fontWeight: FontWeight.w600,
              fontFeatures: [FontFeature.tabularFigures()],
            ),
          ),
        ),
      ],
    ),
  );

  Widget _manualButtons(SlotMeta slot) => Padding(
    padding: const EdgeInsets.only(top: 10),
    child: Row(
      children: [
        Expanded(
          child: OutlinedButton(
            style: OutlinedButton.styleFrom(
              foregroundColor: kGreen,
              side: const BorderSide(color: kGreen, width: 1.5),
              padding: const EdgeInsets.symmetric(vertical: 10),
            ),
            onPressed: () => _manualEntry(slot, 'buy'),
            child: const Text(
              '▲ BUY',
              style: TextStyle(fontWeight: FontWeight.w700, fontSize: 12),
            ),
          ),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: OutlinedButton(
            style: OutlinedButton.styleFrom(
              foregroundColor: kRed,
              side: const BorderSide(color: kRed, width: 1.5),
              padding: const EdgeInsets.symmetric(vertical: 10),
            ),
            onPressed: () => _manualEntry(slot, 'sell'),
            child: const Text(
              '▼ SELL',
              style: TextStyle(fontWeight: FontWeight.w700, fontSize: 12),
            ),
          ),
        ),
      ],
    ),
  );

  Future<void> _toggleTp(SlotMeta slot) async {
    final cfg = (_tp[slot.key] as Map<String, dynamic>?) ?? {};
    final running = cfg['running'] == true;
    try {
      final d = await Api.postJson(
        '/api/tp-monitor/${running ? 'stop' : 'start'}?slot=${slot.key}',
      );
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            d['ok'] == true
                ? '${slot.name} TP monitor ${running ? 'stopped' : 'started'}'
                : 'Error: ${d['error']}',
          ),
        ),
      );
      _refresh();
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text('Error: $e')));
    }
  }

  Future<void> _editTpConfig(SlotMeta slot) async {
    final cfg = (_tp[slot.key] as Map<String, dynamic>?) ?? {};
    final targetCtl = TextEditingController(
      text: fmtNum(cfg['target_pnl'], dp: 0),
    );
    final slCtl = TextEditingController(
      text: fmtNum(cfg['sl_pnl'] ?? 0, dp: 0),
    );
    final tslCtl = TextEditingController(
      text: fmtNum(cfg['tsl_pnl'] ?? 0, dp: 0),
    );
    final pollCtl = TextEditingController(
      text: fmtNum(cfg['poll_secs'], dp: 0),
    );
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: kSurf,
        title: Text('${slot.icon} ${slot.name} TP/SL Config'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: targetCtl,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(labelText: 'Take profit (\$)'),
            ),
            TextField(
              controller: slCtl,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(
                labelText: 'Stop loss (\$)',
                helperText: '0 = off',
              ),
            ),
            TextField(
              controller: tslCtl,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(
                labelText: 'Trailing SL (\$)',
                helperText:
                    '0 = off · arms once profit reaches this, then trails the peak',
              ),
            ),
            TextField(
              controller: pollCtl,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(labelText: 'Poll interval (s)'),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Save', style: TextStyle(color: kGold)),
          ),
        ],
      ),
    );
    if (ok != true) return;
    final body = slot.key == 'morning'
        ? {
            'TP_TARGET_PNL_MORNING': targetCtl.text,
            'TP_POLL_SECS_MORNING': pollCtl.text,
            'SL_TARGET_PNL_MORNING': slCtl.text.isEmpty ? '0' : slCtl.text,
            'TSL_TARGET_PNL_MORNING': tslCtl.text.isEmpty ? '0' : tslCtl.text,
          }
        : {
            'TP_TARGET_PNL': targetCtl.text,
            'TP_POLL_SECS': pollCtl.text,
            'SL_TARGET_PNL': slCtl.text.isEmpty ? '0' : slCtl.text,
            'TSL_TARGET_PNL': tslCtl.text.isEmpty ? '0' : tslCtl.text,
          };
    try {
      await Api.postJson('/api/config', body);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('${slot.name} TP/SL config saved')),
      );
      _refresh();
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text('Error: $e')));
    }
  }

  @override
  Widget build(BuildContext context) {
    final btc = (_evening['btc_futures_price'] as num?)?.toDouble();
    final openSlots = kSlots
        .where((s) => _slotState(s.key)['status'] == 'OPEN')
        .toList();
    final totalPnl = openSlots.fold<double>(
      0,
      (a, s) =>
          a + (((_slotState(s.key)['live_pnl']) as num?)?.toDouble() ?? 0),
    );

    return SafeArea(
      child: RefreshIndicator(
        color: kGold,
        onRefresh: _refresh,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Welcome back, ${Api.displayName.isEmpty ? Api.user : Api.displayName}',
                        style: const TextStyle(
                          color: kText,
                          fontSize: 22,
                          fontWeight: FontWeight.w700,
                          letterSpacing: -.3,
                        ),
                      ),
                      const SizedBox(height: 3),
                      const Text(
                        'Live strategy status and today’s trading activity.',
                        style: TextStyle(color: kMuted, fontSize: 13),
                      ),
                    ],
                  ),
                ),
                const SizedBox(width: 12),
                _StatusPill(
                  openCount: openSlots.length,
                  anyClosed: kSlots.any(
                    (s) => _slotState(s.key)['status'] == 'CLOSED',
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),

            if (btc != null || _wallet['usd_balance'] != null)
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 13,
                      vertical: 7,
                    ),
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(30),
                      color: kSurf,
                      border: Border.all(color: kBorder),
                    ),
                    child: Text(
                      'BTC  ${btc == null ? '—' : '\$${btc.toStringAsFixed(2)}'}  ${_btcUp ? '▲' : '▼'}',
                      style: const TextStyle(
                        color: kText,
                        fontWeight: FontWeight.w700,
                        fontSize: 12.5,
                        fontFeatures: [FontFeature.tabularFigures()],
                      ),
                    ),
                  ),
                  if (_wallet['usd_balance'] != null)
                    Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 13,
                        vertical: 7,
                      ),
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(30),
                        color: kSurf,
                        border: Border.all(color: kBorder),
                      ),
                      child: Text(
                        'Balance  \$${(_wallet['usd_balance'] as num).toStringAsFixed(2)}'
                        '${_wallet['inr_balance'] != null ? ' · ₹${(_wallet['inr_balance'] as num).round()}' : ''}',
                        style: const TextStyle(
                          color: kText,
                          fontWeight: FontWeight.w700,
                          fontSize: 12.5,
                          fontFeatures: [FontFeature.tabularFigures()],
                        ),
                      ),
                    ),
                  if (openSlots.isNotEmpty)
                    Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 13,
                        vertical: 7,
                      ),
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(30),
                        color: (totalPnl >= 0 ? kGreen : kRed).withValues(
                          alpha: .09,
                        ),
                      ),
                      child: Text(
                        'Live P&L  ${fmtUsd(totalPnl)}',
                        style: TextStyle(
                          color: totalPnl >= 0 ? kGreen : kRed,
                          fontWeight: FontWeight.w800,
                          fontSize: 12.5,
                        ),
                      ),
                    ),
                ],
              ),
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
              child: Text(
                "TODAY'S TRADES",
                style: TextStyle(
                  color: kMuted,
                  fontSize: 11,
                  letterSpacing: 1.5,
                ),
              ),
            ),
            if (_todayTrades.isEmpty)
              const Card(
                child: Padding(
                  padding: EdgeInsets.all(20),
                  child: Center(
                    child: Text(
                      'No trades today',
                      style: TextStyle(color: kMuted),
                    ),
                  ),
                ),
              )
            else
              ..._todayTrades.map(
                (t) => _TradeTile(trade: t as Map<String, dynamic>),
              ),
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
                Text(
                  '${slot.icon} ${slot.name.toUpperCase()} TRADE',
                  style: const TextStyle(
                    color: kMuted,
                    fontSize: 11,
                    letterSpacing: 1.2,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const Spacer(),
                Text(
                  slot.entryLabel,
                  style: const TextStyle(color: kGold, fontSize: 10),
                ),
              ],
            ),
            const SizedBox(height: 10),
            if (st['dry_run'] == true) ...[
              _simulatedBody(st),
              if (!open && slot.key == manualSlotNow()) _manualButtons(slot),
            ] else if (open)
              _openBody(slot, st, tpCfg)
            else if (closed) ...[
              _closedBody(st),
              if (slot.key == manualSlotNow()) _manualButtons(slot),
            ] else ...[
              _idleBody(slot),
              if (slot.key == manualSlotNow()) _manualButtons(slot),
            ],
          ],
        ),
      ),
    );
  }

  Widget _simulatedBody(Map<String, dynamic> st) {
    // DRY-RUN: no real order was ever placed, so there are no real numbers
    // to show -- never display simulated $ figures as if they were real money.
    return Row(
      children: [
        const Text('⚠', style: TextStyle(fontSize: 22, color: kGold)),
        const SizedBox(width: 10),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'SIMULATED — no real order was placed',
                style: TextStyle(
                  color: kGold,
                  fontWeight: FontWeight.w700,
                  fontSize: 13,
                ),
              ),
              Text(
                '${st['symbol'] ?? ''} · Mode was DRY RUN at entry time',
                style: const TextStyle(color: kMuted, fontSize: 11),
              ),
            ],
          ),
        ),
      ],
    );
  }

  Widget _openBody(
    SlotMeta slot,
    Map<String, dynamic> st,
    Map<String, dynamic> tpCfg,
  ) {
    final pnl = (st['live_pnl'] as num?)?.toDouble();
    final pnlColor = pnl == null ? kMuted : (pnl >= 0 ? kGreen : kRed);
    final tpRunning = tpCfg['running'] == true;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
              child: Row(
                children: [
                  Flexible(
                    child: Text(
                      st['symbol'] as String? ?? '—',
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: kGold,
                        fontWeight: FontWeight.w700,
                        fontSize: 15,
                      ),
                    ),
                  ),
                  const SizedBox(width: 6),
                  Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 6,
                      vertical: 1,
                    ),
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(
                        color: st['side'] == 'short' ? kRed : kGreen,
                      ),
                    ),
                    child: Text(
                      st['side'] == 'short' ? 'SHORT' : 'LONG',
                      style: TextStyle(
                        color: st['side'] == 'short' ? kRed : kGreen,
                        fontSize: 9,
                        fontWeight: FontWeight.w800,
                        letterSpacing: 1,
                      ),
                    ),
                  ),
                ],
              ),
            ),
            Text(
              fmtUsd(pnl),
              style: TextStyle(
                color: pnlColor,
                fontWeight: FontWeight.w800,
                fontSize: 22,
              ),
            ),
          ],
        ),
        const Divider(color: kBorder),
        _kv('Strike', '\$${fmtNum(st['strike'])}'),
        _kv('Lots', '${st['lots'] ?? '—'}'),
        _kv('Entry Mark', fmtUsd(st['entry_mark'], dp: 4)),
        _kv('Current Mark', fmtUsd(st['current_mark'], dp: 4)),
        _kv('Total Cost', fmtUsd(st['total_cost_usd'])),
        _kv(
          'Settlement',
          (st['settlement'] as String? ?? '')
              .replaceAll('T', ' ')
              .replaceAll('Z', ' UTC'),
        ),
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
                child: const Text(
                  '⏹ SQUARE OFF',
                  style: TextStyle(fontWeight: FontWeight.w700),
                ),
              ),
            ),
            const SizedBox(width: 10),
            OutlinedButton(
              style: OutlinedButton.styleFrom(
                foregroundColor: kGold,
                side: const BorderSide(color: kGold, width: 1.5),
                padding: const EdgeInsets.symmetric(
                  vertical: 11,
                  horizontal: 14,
                ),
              ),
              onPressed: () => _showPayoff(st),
              child: const Icon(Icons.ssid_chart, size: 20),
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
                    const Text(
                      'TP / SL / TSL MONITOR',
                      style: TextStyle(
                        color: kMuted,
                        fontSize: 9,
                        letterSpacing: 1,
                      ),
                    ),
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
                      'TP ${fmtUsd(tpCfg['target_pnl'], dp: 0)}'
                      ' · SL ${((tpCfg['sl_pnl'] as num?) ?? 0) > 0 ? fmtUsd(tpCfg['sl_pnl'], dp: 0) : 'off'}'
                      ' · TSL ${((tpCfg['tsl_pnl'] as num?) ?? 0) > 0 ? fmtUsd(tpCfg['tsl_pnl'], dp: 0) : 'off'}'
                      ' · ${fmtNum(tpCfg['poll_secs'])}s',
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
                  backgroundColor: tpRunning
                      ? kRed.withValues(alpha: 0.15)
                      : kGreen.withValues(alpha: 0.15),
                  foregroundColor: tpRunning ? kRed : kGreen,
                  padding: const EdgeInsets.symmetric(horizontal: 14),
                ),
                onPressed: () => _toggleTp(slot),
                child: Text(
                  tpRunning ? 'STOP' : 'START',
                  style: const TextStyle(fontSize: 12),
                ),
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
              Text(
                'Closed ${st['exit_time_utc'] ?? ''} UTC · ${st['symbol'] ?? ''}',
                style: const TextStyle(color: kMuted, fontSize: 12),
              ),
              Text(
                'P&L ${fmtUsd(pnl)}',
                style: TextStyle(
                  color: pnl >= 0 ? kGreen : kRed,
                  fontWeight: FontWeight.w700,
                  fontSize: 16,
                ),
              ),
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
        Text(
          'Waiting for entry — ${slot.entryLabel}',
          style: const TextStyle(color: kMuted, fontSize: 13),
        ),
      ],
    );
  }

  Widget _kv(String k, String v) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 3),
    child: Row(
      children: [
        Text(k, style: const TextStyle(color: kMuted, fontSize: 12.5)),
        const Spacer(),
        Text(
          v,
          style: const TextStyle(
            color: kText,
            fontSize: 12.5,
            fontFeatures: [FontFeature.tabularFigures()],
          ),
        ),
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
      child: Text(
        label,
        style: TextStyle(
          color: color,
          fontSize: 10,
          fontWeight: FontWeight.w700,
          letterSpacing: 1,
        ),
      ),
    );
  }
}

class _TradeTile extends StatelessWidget {
  final Map<String, dynamic> trade;
  const _TradeTile({required this.trade});

  @override
  Widget build(BuildContext context) {
    final live = trade['_live'] == true;
    final isDryRun = trade['dry_run'] == true;
    final slotIcon = trade['slot'] == 'morning'
        ? '🌅 '
        : (trade['slot'] == 'evening' ? '🌇 ' : '');

    // DRY-RUN: no real order was placed, so no real numbers exist to show.
    if (isDryRun) {
      return Card(
        child: ListTile(
          title: Text(
            '$slotIcon${trade['symbol'] ?? '—'}',
            style: const TextStyle(
              color: kGold,
              fontWeight: FontWeight.w600,
              fontSize: 14,
            ),
          ),
          subtitle: const Text(
            'No real order was placed',
            style: TextStyle(color: kMuted, fontSize: 12),
          ),
          trailing: Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: kGold),
              color: kGold.withValues(alpha: 0.08),
            ),
            child: const Text(
              '⚠ SIMULATED',
              style: TextStyle(
                color: kGold,
                fontSize: 10,
                fontWeight: FontWeight.w800,
              ),
            ),
          ),
        ),
      );
    }

    final pnl = ((live ? trade['live_pnl'] : trade['pnl_usd']) as num?)
        ?.toDouble();
    final pnlColor = pnl == null ? kMuted : (pnl >= 0 ? kGreen : kRed);
    return Card(
      child: ListTile(
        title: Text(
          '$slotIcon${trade['symbol'] ?? '—'}',
          style: TextStyle(
            color: live ? kGold : kText,
            fontWeight: FontWeight.w600,
            fontSize: 14,
          ),
        ),
        subtitle: Text(
          '${trade['lots'] ?? ''} lots  ·  entry ${fmtUsd(trade['entry_mark'], dp: 4)}',
          style: const TextStyle(color: kMuted, fontSize: 12),
        ),
        trailing: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Text(
              fmtUsd(pnl),
              style: TextStyle(
                color: pnlColor,
                fontWeight: FontWeight.w700,
                fontSize: 15,
              ),
            ),
            Text(
              live ? 'LIVE' : (pnl != null && pnl >= 0 ? 'WIN' : 'LOSS'),
              style: TextStyle(
                color: live ? kGold : pnlColor,
                fontSize: 10,
                fontWeight: FontWeight.w700,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────
// Logs page
// ─────────────────────────────────────────────────────────────
class _PageIntro extends StatelessWidget {
  final String title;
  final String subtitle;
  final VoidCallback? onRefresh;
  const _PageIntro(this.title, this.subtitle, {this.onRefresh});

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.fromLTRB(4, 4, 4, 16),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                title,
                style: const TextStyle(
                  color: kText,
                  fontSize: 22,
                  fontWeight: FontWeight.w700,
                  letterSpacing: -.3,
                ),
              ),
              const SizedBox(height: 3),
              Text(
                subtitle,
                style: const TextStyle(color: kMuted, fontSize: 13),
              ),
            ],
          ),
        ),
        if (onRefresh != null)
          IconButton.filledTonal(
            onPressed: onRefresh,
            tooltip: 'Refresh',
            icon: const Icon(Icons.refresh, color: kGold),
          ),
      ],
    ),
  );
}

class _MetricCard extends StatelessWidget {
  final String label;
  final String value;
  final Color? color;
  final IconData icon;
  const _MetricCard(this.label, this.value, this.icon, {this.color});

  @override
  Widget build(BuildContext context) => Card(
    child: Padding(
      padding: const EdgeInsets.all(15),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(icon, size: 17, color: color ?? kGold),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  label.toUpperCase(),
                  style: const TextStyle(
                    color: kMuted,
                    fontSize: 10,
                    fontWeight: FontWeight.w700,
                    letterSpacing: .8,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Text(
            value,
            style: TextStyle(
              color: color ?? kText,
              fontSize: 19,
              fontWeight: FontWeight.w700,
              fontFeatures: const [FontFeature.tabularFigures()],
            ),
          ),
        ],
      ),
    ),
  );
}

class _EmptyCard extends StatelessWidget {
  final IconData icon;
  final String title;
  final String subtitle;
  const _EmptyCard(this.icon, this.title, this.subtitle);

  @override
  Widget build(BuildContext context) => Card(
    child: Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 34),
      child: Center(
        child: Column(
          children: [
            Icon(icon, size: 32, color: kMuted),
            const SizedBox(height: 10),
            Text(
              title,
              style: const TextStyle(color: kText, fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 4),
            Text(
              subtitle,
              textAlign: TextAlign.center,
              style: const TextStyle(color: kMuted, fontSize: 12),
            ),
          ],
        ),
      ),
    ),
  );
}

// ─────────────────────────────────────────────────────────────
// Trades & P&L — mirrors templates/trades.html
// ─────────────────────────────────────────────────────────────
class TradesPage extends StatefulWidget {
  const TradesPage({super.key});
  @override
  State<TradesPage> createState() => _TradesPageState();
}

class _TradesPageState extends State<TradesPage> {
  List<Map<String, dynamic>> _trades = [];
  Map<String, dynamic> _summary = {};
  String _filter = 'all';
  String? _error;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    if (mounted) setState(() => _loading = true);
    try {
      final data = await Future.wait([
        Api.getJson('/api/trades'),
        Api.getJson('/api/summary'),
      ]);
      if (!mounted) return;
      setState(() {
        _trades = (data[0] as List)
            .map((e) => Map<String, dynamic>.from(e as Map))
            .toList()
            .reversed
            .toList();
        _summary = Map<String, dynamic>.from(data[1] as Map);
        _error = null;
      });
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  List<Map<String, dynamic>> get _visible => _trades.where((t) {
    final p = (t['pnl_usd'] as num?)?.toDouble() ?? 0;
    return _filter == 'all' ||
        (_filter == 'wins' && p >= 0) ||
        (_filter == 'losses' && p < 0);
  }).toList();

  @override
  Widget build(BuildContext context) => SafeArea(
    child: RefreshIndicator(
      color: kGold,
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          _PageIntro(
            'Trades & P&L',
            'Complete live trade history and account performance.',
            onRefresh: _load,
          ),
          if (_error != null)
            Card(
              child: Padding(
                padding: const EdgeInsets.all(14),
                child: Text(_error!, style: const TextStyle(color: kRed)),
              ),
            ),
          LayoutBuilder(
            builder: (_, c) {
              final width = (c.maxWidth - 12) / 2;
              final net = (_summary['total_pnl'] as num?)?.toDouble() ?? 0;
              return Wrap(
                spacing: 12,
                runSpacing: 12,
                children: [
                  SizedBox(
                    width: width,
                    child: _MetricCard(
                      'Net P&L',
                      fmtUsd(net),
                      Icons.account_balance_wallet_outlined,
                      color: net >= 0 ? kGreen : kRed,
                    ),
                  ),
                  SizedBox(
                    width: width,
                    child: _MetricCard(
                      'Win rate',
                      '${fmtNum(_summary['win_rate'], dp: 1)}%',
                      Icons.track_changes_outlined,
                    ),
                  ),
                  SizedBox(
                    width: width,
                    child: _MetricCard(
                      'Trades',
                      fmtNum(_summary['total_days']),
                      Icons.swap_vert_outlined,
                    ),
                  ),
                  SizedBox(
                    width: width,
                    child: _MetricCard(
                      'Max drawdown',
                      fmtUsd(_summary['max_dd']),
                      Icons.trending_down,
                      color: kRed,
                    ),
                  ),
                ],
              );
            },
          ),
          const SizedBox(height: 16),
          SegmentedButton<String>(
            segments: const [
              ButtonSegment(value: 'all', label: Text('All')),
              ButtonSegment(
                value: 'wins',
                label: Text('Wins'),
                icon: Icon(Icons.arrow_upward, size: 15),
              ),
              ButtonSegment(
                value: 'losses',
                label: Text('Losses'),
                icon: Icon(Icons.arrow_downward, size: 15),
              ),
            ],
            selected: {_filter},
            onSelectionChanged: (v) => setState(() => _filter = v.first),
          ),
          const SizedBox(height: 12),
          if (_loading)
            const Padding(
              padding: EdgeInsets.all(40),
              child: Center(child: CircularProgressIndicator(color: kGold)),
            )
          else if (_visible.isEmpty)
            const _EmptyCard(
              Icons.show_chart,
              'No trades to show',
              'Completed trades will appear here automatically.',
            )
          else
            ..._visible.map(_tradeCard),
          const SizedBox(height: 20),
        ],
      ),
    ),
  );

  Widget _tradeCard(Map<String, dynamic> t) {
    final pnl = (t['pnl_usd'] as num?)?.toDouble() ?? 0;
    final side = (t['side'] ?? '').toString().toUpperCase();
    final date = (t['date'] ?? t['entry_date'] ?? '—').toString();
    final entryTime = (t['entry_time'] ?? t['entry_time_utc'] ?? '').toString();
    final exitTime = (t['exit_time'] ?? t['exit_time_utc'] ?? '').toString();
    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: Padding(
        padding: const EdgeInsets.all(15),
        child: Column(
          children: [
            Row(
              children: [
                Container(
                  width: 4,
                  height: 38,
                  decoration: BoxDecoration(
                    color: pnl >= 0 ? kGreen : kRed,
                    borderRadius: BorderRadius.circular(4),
                  ),
                ),
                const SizedBox(width: 11),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        (t['symbol'] ?? 'Unknown product').toString(),
                        style: const TextStyle(
                          color: kText,
                          fontWeight: FontWeight.w700,
                          fontSize: 14,
                        ),
                      ),
                      Text(
                        '$date${entryTime.isNotEmpty ? ' · ${utcToIstText(entryTime)}' : ''}',
                        style: const TextStyle(color: kMuted, fontSize: 11.5),
                      ),
                    ],
                  ),
                ),
                Column(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    Text(
                      fmtUsd(pnl),
                      style: TextStyle(
                        color: pnl >= 0 ? kGreen : kRed,
                        fontSize: 16,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                    Text(
                      side.isEmpty
                          ? (t['slot'] ?? '').toString().toUpperCase()
                          : side,
                      style: const TextStyle(
                        color: kMuted,
                        fontSize: 10,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ],
                ),
              ],
            ),
            const Divider(height: 22),
            Row(
              children: [
                Expanded(
                  child: _miniValue('Entry', fmtUsd(t['entry_mark'], dp: 4)),
                ),
                Expanded(
                  child: _miniValue('Exit', fmtUsd(t['exit_mark'], dp: 4)),
                ),
                Expanded(child: _miniValue('Lots', fmtNum(t['lots']))),
                Expanded(
                  child: _miniValue(
                    'Exit time',
                    exitTime.isEmpty ? '—' : utcToIstText(exitTime),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _miniValue(String k, String v) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text(
        k.toUpperCase(),
        style: const TextStyle(
          color: kMuted,
          fontSize: 9,
          fontWeight: FontWeight.w700,
        ),
      ),
      const SizedBox(height: 3),
      Text(
        v,
        overflow: TextOverflow.ellipsis,
        style: const TextStyle(
          color: kText,
          fontSize: 11.5,
          fontWeight: FontWeight.w600,
          fontFeatures: [FontFeature.tabularFigures()],
        ),
      ),
    ],
  );
}

String utcToIstText(String hhmmss) {
  try {
    final p = hhmmss.split(':').map(int.parse).toList();
    final t = (p[0] * 60 + p[1] + 330) % 1440;
    final h = t ~/ 60, m = t % 60;
    return '${(h + 11) % 12 + 1}:${m.toString().padLeft(2, '0')} ${h >= 12 ? 'PM' : 'AM'}';
  } catch (_) {
    return hhmmss;
  }
}

// ─────────────────────────────────────────────────────────────
// Positions — mirrors templates/positions.html
// ─────────────────────────────────────────────────────────────
class PositionsPage extends StatefulWidget {
  const PositionsPage({super.key});
  @override
  State<PositionsPage> createState() => _PositionsPageState();
}

class _PositionsPageState extends State<PositionsPage> {
  List<Map<String, dynamic>> _positions = [];
  Map<String, dynamic> _wallet = {};
  String? _error;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    if (mounted) setState(() => _loading = true);
    try {
      final data = await Future.wait([
        Api.getJson('/api/all-positions'),
        Api.getJson('/api/wallet'),
      ]);
      if (!mounted) return;
      setState(() {
        _positions = data[0] is List
            ? (data[0] as List)
                  .map((e) => Map<String, dynamic>.from(e as Map))
                  .toList()
            : [];
        _wallet = data[1] is Map
            ? Map<String, dynamic>.from(data[1] as Map)
            : {};
        _error = null;
      });
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final livePnl = _positions.fold<double>(
      0,
      (a, p) => a + ((p['live_pnl'] as num?)?.toDouble() ?? 0),
    );
    return SafeArea(
      child: RefreshIndicator(
        color: kGold,
        onRefresh: _load,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            _PageIntro(
              'Open Positions',
              'Every live Delta position on this account.',
              onRefresh: _load,
            ),
            if (_error != null)
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(14),
                  child: Text(_error!, style: const TextStyle(color: kRed)),
                ),
              ),
            LayoutBuilder(
              builder: (_, c) {
                final w = (c.maxWidth - 12) / 2;
                return Wrap(
                  spacing: 12,
                  runSpacing: 12,
                  children: [
                    SizedBox(
                      width: w,
                      child: _MetricCard(
                        'USD balance',
                        fmtUsd(_wallet['usd_balance']),
                        Icons.account_balance_outlined,
                      ),
                    ),
                    SizedBox(
                      width: w,
                      child: _MetricCard(
                        'Available',
                        fmtUsd(_wallet['usd_available']),
                        Icons.payments_outlined,
                      ),
                    ),
                    SizedBox(
                      width: w,
                      child: _MetricCard(
                        'Open positions',
                        '${_positions.length}',
                        Icons.view_list_outlined,
                      ),
                    ),
                    SizedBox(
                      width: w,
                      child: _MetricCard(
                        'Live P&L',
                        fmtUsd(livePnl),
                        Icons.query_stats,
                        color: livePnl >= 0 ? kGreen : kRed,
                      ),
                    ),
                  ],
                );
              },
            ),
            const SizedBox(height: 16),
            if (_loading)
              const Padding(
                padding: EdgeInsets.all(40),
                child: Center(child: CircularProgressIndicator(color: kGold)),
              )
            else if (_positions.isEmpty)
              const _EmptyCard(
                Icons.inbox_outlined,
                'No open positions',
                'The account is currently flat.',
              )
            else
              ..._positions.map(_positionCard),
            const SizedBox(height: 20),
          ],
        ),
      ),
    );
  }

  Widget _positionCard(Map<String, dynamic> p) {
    final pnl = (p['live_pnl'] as num?)?.toDouble() ?? 0;
    final long = (p['side'] ?? '').toString().toUpperCase() == 'LONG';
    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: Padding(
        padding: const EdgeInsets.all(15),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 8,
                    vertical: 4,
                  ),
                  decoration: BoxDecoration(
                    color: (long ? kGreen : kRed).withValues(alpha: .09),
                    borderRadius: BorderRadius.circular(6),
                  ),
                  child: Text(
                    long ? 'LONG' : 'SHORT',
                    style: TextStyle(
                      color: long ? kGreen : kRed,
                      fontSize: 10,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                ),
                const SizedBox(width: 9),
                Expanded(
                  child: Text(
                    (p['symbol'] ?? '').toString(),
                    style: const TextStyle(
                      color: kText,
                      fontWeight: FontWeight.w700,
                      fontSize: 14,
                    ),
                  ),
                ),
                Text(
                  fmtUsd(pnl),
                  style: TextStyle(
                    color: pnl >= 0 ? kGreen : kRed,
                    fontWeight: FontWeight.w800,
                    fontSize: 16,
                  ),
                ),
              ],
            ),
            const Divider(height: 22),
            Row(
              children: [
                Expanded(child: _positionValue('Size', fmtNum(p['size']))),
                Expanded(
                  child: _positionValue(
                    'Entry',
                    fmtUsd(p['entry_price'], dp: 4),
                  ),
                ),
                Expanded(
                  child: _positionValue('Mark', fmtUsd(p['mark_price'], dp: 4)),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _positionValue(String k, String v) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text(
        k.toUpperCase(),
        style: const TextStyle(
          color: kMuted,
          fontSize: 9,
          fontWeight: FontWeight.w700,
        ),
      ),
      const SizedBox(height: 4),
      Text(
        v,
        style: const TextStyle(
          color: kText,
          fontWeight: FontWeight.w600,
          fontSize: 12,
          fontFeatures: [FontFeature.tabularFigures()],
        ),
      ),
    ],
  );
}

// ─────────────────────────────────────────────────────────────
// API Accounts — mirrors templates/accounts.html
// ─────────────────────────────────────────────────────────────
class AccountsPage extends StatefulWidget {
  const AccountsPage({super.key});
  @override
  State<AccountsPage> createState() => _AccountsPageState();
}

class _AccountsPageState extends State<AccountsPage> {
  List<Map<String, dynamic>> _accounts = [];
  Map<String, dynamic> _bots = {};
  String? _error;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    if (mounted) setState(() => _loading = true);
    try {
      final data = await Future.wait([
        Api.getJson('/api/accounts'),
        Api.getJson('/api/bots').catchError((_) => <String, dynamic>{}),
      ]);
      if (!mounted) return;
      setState(() {
        _accounts = (data[0] as List)
            .map((e) => Map<String, dynamic>.from(e as Map))
            .toList();
        _bots = data[1] is Map ? Map<String, dynamic>.from(data[1] as Map) : {};
        _error = null;
      });
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _edit([Map<String, dynamic>? account]) async {
    final user = TextEditingController(
      text: (account?['username'] ?? '').toString(),
    );
    final name = TextEditingController(
      text: (account?['display_name'] ?? '').toString(),
    );
    final password = TextEditingController();
    final key = TextEditingController();
    final secret = TextEditingController();
    bool busy = false;
    String? message;
    final saved = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setLocal) => AlertDialog(
          title: Text(
            account == null ? 'Add API account' : 'Edit ${account['username']}',
          ),
          content: SizedBox(
            width: 430,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: user,
                    enabled: account == null,
                    decoration: const InputDecoration(labelText: 'Username'),
                  ),
                  const SizedBox(height: 10),
                  TextField(
                    controller: name,
                    decoration: const InputDecoration(
                      labelText: 'Display name',
                    ),
                  ),
                  const SizedBox(height: 10),
                  TextField(
                    controller: password,
                    obscureText: true,
                    decoration: InputDecoration(
                      labelText: account == null
                          ? 'Password'
                          : 'New password (optional)',
                    ),
                  ),
                  const SizedBox(height: 10),
                  TextField(
                    controller: key,
                    decoration: const InputDecoration(
                      labelText: 'Delta API key',
                    ),
                  ),
                  const SizedBox(height: 10),
                  TextField(
                    controller: secret,
                    obscureText: true,
                    decoration: const InputDecoration(
                      labelText: 'Delta API secret',
                    ),
                  ),
                  if (message != null) ...[
                    const SizedBox(height: 10),
                    Align(
                      alignment: Alignment.centerLeft,
                      child: Text(
                        message!,
                        style: TextStyle(
                          color: message!.startsWith('✓') ? kGreen : kRed,
                          fontSize: 12,
                        ),
                      ),
                    ),
                  ],
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: busy ? null : () => Navigator.pop(ctx, false),
              child: const Text('Cancel'),
            ),
            TextButton(
              onPressed: busy
                  ? null
                  : () async {
                      setLocal(() {
                        busy = true;
                        message = null;
                      });
                      try {
                        final d = await Api.postJson('/api/accounts/test', {
                          'username': user.text.trim(),
                          'api_key': key.text.trim(),
                          'api_secret': secret.text.trim(),
                        });
                        setLocal(
                          () => message = d['ok'] == true
                              ? '✓ Connected · USD ${fmtNum(d['usd_balance'], dp: 2)}'
                              : 'Connection failed: ${d['error']}',
                        );
                      } catch (e) {
                        setLocal(() => message = 'Connection failed: $e');
                      } finally {
                        setLocal(() => busy = false);
                      }
                    },
              child: const Text('Test connection'),
            ),
            FilledButton(
              onPressed: busy
                  ? null
                  : () async {
                      setLocal(() => busy = true);
                      try {
                        final d = await Api.postJson('/api/accounts', {
                          'username': user.text.trim(),
                          'display_name': name.text.trim(),
                          'password': password.text,
                          'api_key': key.text.trim(),
                          'api_secret': secret.text.trim(),
                        });
                        if (d['ok'] == true && ctx.mounted) {
                          Navigator.pop(ctx, true);
                        } else {
                          setLocal(
                            () => message =
                                'Save failed: ${d['error'] ?? 'Unknown error'}',
                          );
                        }
                      } catch (e) {
                        setLocal(() => message = 'Save failed: $e');
                      } finally {
                        if (ctx.mounted) setLocal(() => busy = false);
                      }
                    },
              child: Text(busy ? 'Saving…' : 'Save'),
            ),
          ],
        ),
      ),
    );
    user.dispose();
    name.dispose();
    password.dispose();
    key.dispose();
    secret.dispose();
    if (saved == true) {
      await _load();
      _toast('Account saved');
    }
  }

  Future<void> _botAction(String user, bool active) async {
    final d = await Api.postJson(
      '/api/bots/$user/${active ? 'stop' : 'start'}',
    );
    if (d['ok'] == true) {
      _toast('Bot ${active ? 'stopped' : 'started'}');
      await _load();
    } else {
      _toast((d['error'] ?? 'Bot action failed').toString(), error: true);
    }
  }

  Future<void> _delete(String user) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete account?'),
        content: Text(
          'Remove the login for “$user”? Trade history remains on the server.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: kRed),
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    final d = await Api.deleteJson(
      '/api/accounts/${Uri.encodeComponent(user)}',
    );
    if (d['ok'] == true) {
      _toast('Account removed');
      await _load();
    } else {
      _toast((d['error'] ?? 'Delete failed').toString(), error: true);
    }
  }

  void _toast(String text, {bool error = false}) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(text), backgroundColor: error ? kRed : kGreen),
    );
  }

  @override
  Widget build(BuildContext context) => SafeArea(
    child: RefreshIndicator(
      color: kGold,
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          _PageIntro(
            'API Accounts',
            'Manage Delta credentials and one bot instance per account.',
            onRefresh: _load,
          ),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: Row(
                children: [
                  const Icon(Icons.security_outlined, color: kGold),
                  const SizedBox(width: 10),
                  const Expanded(
                    child: Text(
                      'API secrets remain on the server and are never displayed after saving.',
                      style: TextStyle(color: kMuted, fontSize: 12.5),
                    ),
                  ),
                  FilledButton.icon(
                    onPressed: () => _edit(),
                    icon: const Icon(Icons.add, size: 18),
                    label: const Text('Add account'),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 12),
          if (_error != null)
            Card(
              child: Padding(
                padding: const EdgeInsets.all(14),
                child: Text(_error!, style: const TextStyle(color: kRed)),
              ),
            ),
          if (_loading)
            const Padding(
              padding: EdgeInsets.all(40),
              child: Center(child: CircularProgressIndicator(color: kGold)),
            )
          else if (_accounts.isEmpty)
            const _EmptyCard(
              Icons.manage_accounts_outlined,
              'No accounts',
              'Add a Delta account to begin.',
            )
          else
            ..._accounts.map(_accountCard),
          const SizedBox(height: 20),
        ],
      ),
    ),
  );

  Widget _accountCard(Map<String, dynamic> a) {
    final user = (a['username'] ?? '').toString();
    final bot = _bots[user] is Map
        ? Map<String, dynamic>.from(_bots[user] as Map)
        : <String, dynamic>{};
    final supported = bot['supported'] == true;
    final active = bot['active'] == true;
    final primary = a['primary'] == true;
    final protected = primary || a['bot'] == true;
    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: Padding(
        padding: const EdgeInsets.all(15),
        child: Column(
          children: [
            Row(
              children: [
                CircleAvatar(
                  backgroundColor: const Color(0xFFEEF3FE),
                  child: Text(
                    ((a['display_name'] ?? user).toString().isEmpty
                            ? '?'
                            : (a['display_name'] ?? user).toString()[0])
                        .toUpperCase(),
                    style: const TextStyle(
                      color: kGold,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                ),
                const SizedBox(width: 11),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Flexible(
                            child: Text(
                              (a['display_name'] ?? user).toString(),
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                color: kText,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          ),
                          if (primary)
                            const Padding(
                              padding: EdgeInsets.only(left: 7),
                              child: Icon(
                                Icons.shield_outlined,
                                size: 15,
                                color: kGold,
                              ),
                            ),
                        ],
                      ),
                      Text(
                        '${primary ? 'PRIMARY' : 'COEXISTENT'} · @$user · ${a['api_key'] ?? 'No key'}',
                        style: const TextStyle(color: kMuted, fontSize: 11.5),
                      ),
                    ],
                  ),
                ),
                if (supported)
                  Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 8,
                      vertical: 4,
                    ),
                    decoration: BoxDecoration(
                      color: (active ? kGreen : kMuted).withValues(alpha: .10),
                      borderRadius: BorderRadius.circular(20),
                    ),
                    child: Text(
                      active ? 'RUNNING' : 'STOPPED',
                      style: TextStyle(
                        color: active ? kGreen : kMuted,
                        fontSize: 9,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                  ),
              ],
            ),
            const Divider(height: 22),
            Row(
              children: [
                OutlinedButton.icon(
                  onPressed: () => _edit(a),
                  icon: const Icon(Icons.edit_outlined, size: 16),
                  label: const Text('Edit'),
                ),
                if (supported) ...[
                  const SizedBox(width: 8),
                  OutlinedButton.icon(
                    onPressed: () => _botAction(user, active),
                    icon: Icon(
                      active
                          ? Icons.stop_circle_outlined
                          : Icons.play_circle_outline,
                      size: 17,
                    ),
                    label: Text(active ? 'Stop bot' : 'Start bot'),
                  ),
                ],
                const Spacer(),
                IconButton(
                  onPressed: protected ? null : () => _delete(user),
                  tooltip: 'Delete account',
                  icon: const Icon(Icons.delete_outline, color: kRed),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────
// Configs page — every bot setting, grouped; times shown in IST
// ─────────────────────────────────────────────────────────────
(int, int) utcToIst(int h, int m) {
  final t = (h * 60 + m + 330) % 1440;
  return (t ~/ 60, t % 60);
}

(int, int) istToUtc(int h, int m) {
  final t = ((h * 60 + m - 330) % 1440 + 1440) % 1440;
  return (t ~/ 60, t % 60);
}

class ConfigsPage extends StatefulWidget {
  const ConfigsPage({super.key});

  @override
  State<ConfigsPage> createState() => _ConfigsPageState();
}

class _ConfigsPageState extends State<ConfigsPage> {
  final Map<String, TextEditingController> _ctl = {};
  bool _dryRun = false;
  bool _morningEnabled = true;
  bool _morningExitEnabled = true;
  bool _eveningEnabled = true;
  bool _eveningExitEnabled = true;
  String _morningSide = 'buy';
  String _eveningSide = 'buy';
  bool _telegramAlerts = true;
  bool _dynamicLots = true;
  bool _loading = true;
  bool _saving = false;
  String? _error;

  // Text-field keys (times handled separately as IST pairs).
  // TP/SL/TSL and poll are deliberately NOT here — the monitor is configured
  // on the Dashboard tab, on the running trade's own card.
  static const _numKeys = [
    'STRADDLE_LOTS',
    'MORNING_LOTS',
    'MAX_TRADES_PER_DAY',
    'STRIKE_STEP',
  ];
  static const _timePairs = {
    'entry': ('ENTRY_H_UTC', 'ENTRY_M_UTC'),
    'exit': ('EXIT_H_UTC', 'EXIT_M_UTC'),
    'morning': ('MORNING_H_UTC', 'MORNING_M_UTC'),
    'morning_exit': ('MORNING_EXIT_H_UTC', 'MORNING_EXIT_M_UTC'),
  };

  @override
  void initState() {
    super.initState();
    for (final k in _numKeys) {
      _ctl[k] = TextEditingController();
    }
    for (final pair in _timePairs.keys) {
      _ctl['${pair}_h'] = TextEditingController();
      _ctl['${pair}_m'] = TextEditingController();
    }
    _load();
  }

  @override
  void dispose() {
    for (final c in _ctl.values) {
      c.dispose();
    }
    super.dispose();
  }

  bool _envBool(dynamic v, {bool dflt = true}) {
    final s = (v ?? '').toString().trim().toLowerCase();
    if (s.isEmpty) return dflt;
    return s == '1' || s == 'true' || s == 'yes';
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final d = await Api.getJson('/api/config') as Map<String, dynamic>;
      for (final k in _numKeys) {
        _ctl[k]!.text = (d[k] ?? '').toString();
      }
      _timePairs.forEach((pair, keys) {
        final h = int.tryParse((d[keys.$1] ?? '').toString());
        final m = int.tryParse((d[keys.$2] ?? '').toString());
        if (h != null && m != null) {
          final (ih, im) = utcToIst(h, m);
          _ctl['${pair}_h']!.text = ih.toString();
          _ctl['${pair}_m']!.text = im.toString().padLeft(2, '0');
        }
      });
      _dryRun = _envBool(d['DRY_RUN'], dflt: false);
      _morningEnabled = _envBool(d['MORNING_ENABLED']);
      _morningExitEnabled = _envBool(d['MORNING_EXIT_ENABLED']);
      _eveningEnabled = _envBool(d['EVENING_ENABLED']);
      _eveningExitEnabled = _envBool(d['EVENING_EXIT_ENABLED']);
      _morningSide =
          (d['MORNING_SIDE'] ?? '').toString().toLowerCase() == 'sell'
          ? 'sell'
          : 'buy';
      _eveningSide =
          (d['EVENING_SIDE'] ?? '').toString().toLowerCase() == 'sell'
          ? 'sell'
          : 'buy';
      _telegramAlerts = _envBool(d['TELEGRAM_ALERTS']);
      _dynamicLots = _envBool(d['DYNAMIC_LOTS']);
      setState(() => _loading = false);
    } catch (e) {
      setState(() {
        _loading = false;
        _error = 'Cannot load config: $e';
      });
    }
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    final body = <String, dynamic>{
      'DRY_RUN': _dryRun ? 'true' : 'false',
      'MORNING_ENABLED': _morningEnabled ? 'true' : 'false',
      'MORNING_EXIT_ENABLED': _morningExitEnabled ? 'true' : 'false',
      'EVENING_ENABLED': _eveningEnabled ? 'true' : 'false',
      'EVENING_EXIT_ENABLED': _eveningExitEnabled ? 'true' : 'false',
      'MORNING_SIDE': _morningSide,
      'EVENING_SIDE': _eveningSide,
      'TELEGRAM_ALERTS': _telegramAlerts ? 'true' : 'false',
      'DYNAMIC_LOTS': _dynamicLots ? 'true' : 'false',
    };
    for (final k in _numKeys) {
      final v = _ctl[k]!.text.trim();
      if (v.isNotEmpty) body[k] = v;
    }
    _timePairs.forEach((pair, keys) {
      final h = int.tryParse(_ctl['${pair}_h']!.text);
      final m = int.tryParse(_ctl['${pair}_m']!.text);
      if (h != null && m != null) {
        final (uh, um) = istToUtc(h, m);
        body[keys.$1] = uh.toString();
        body[keys.$2] = um.toString();
      }
    });
    try {
      final d = await Api.postJson('/api/config', body);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            d['ok'] == true
                ? 'Saved ✓ — restart the bot service to apply times/lots'
                : 'Save failed: ${d['error']}',
          ),
          backgroundColor: d['ok'] == true ? kGreen : kRed,
        ),
      );
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text('Error: $e')));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Widget _section(String title) => Padding(
    padding: const EdgeInsets.only(left: 4, top: 18, bottom: 8),
    child: Text(
      title,
      style: const TextStyle(color: kMuted, fontSize: 11, letterSpacing: 1.5),
    ),
  );

  Widget _numField(String key, String label) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 6),
    child: TextField(
      controller: _ctl[key],
      keyboardType: TextInputType.number,
      decoration: InputDecoration(
        labelText: label,
        isDense: true,
        border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
      ),
    ),
  );

  Widget _timeField(String pair, String label) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 6),
    child: Row(
      children: [
        Expanded(
          flex: 3,
          child: Text(
            label,
            style: const TextStyle(color: kText, fontSize: 13.5),
          ),
        ),
        Expanded(
          flex: 2,
          child: TextField(
            controller: _ctl['${pair}_h'],
            keyboardType: TextInputType.number,
            textAlign: TextAlign.center,
            decoration: InputDecoration(
              labelText: 'HH',
              isDense: true,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(8),
              ),
            ),
          ),
        ),
        const Padding(
          padding: EdgeInsets.symmetric(horizontal: 6),
          child: Text(':', style: TextStyle(color: kMuted, fontSize: 18)),
        ),
        Expanded(
          flex: 2,
          child: TextField(
            controller: _ctl['${pair}_m'],
            keyboardType: TextInputType.number,
            textAlign: TextAlign.center,
            decoration: InputDecoration(
              labelText: 'MM',
              isDense: true,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(8),
              ),
            ),
          ),
        ),
        const SizedBox(width: 6),
        const Text('IST', style: TextStyle(color: kGold, fontSize: 11)),
      ],
    ),
  );

  Widget _switchTile(
    String label,
    bool value,
    ValueChanged<bool> onChanged, {
    String? subtitle,
  }) => SwitchListTile(
    title: Text(label, style: const TextStyle(fontSize: 14)),
    subtitle: subtitle == null
        ? null
        : Text(subtitle, style: const TextStyle(color: kMuted, fontSize: 11)),
    value: value,
    activeThumbColor: kGold,
    contentPadding: EdgeInsets.zero,
    onChanged: onChanged,
  );

  Widget _sideField(String value, ValueChanged<String> onChanged) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 6),
    child: DropdownButtonFormField<String>(
      initialValue: value,
      decoration: InputDecoration(
        labelText: 'Direction',
        isDense: true,
        border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
      ),
      dropdownColor: kSurf,
      items: const [
        DropdownMenuItem(
          value: 'buy',
          child: Text(
            'BUY straddle — long (big move)',
            style: TextStyle(fontSize: 13.5),
          ),
        ),
        DropdownMenuItem(
          value: 'sell',
          child: Text(
            'SELL straddle — short (premium)',
            style: TextStyle(fontSize: 13.5),
          ),
        ),
      ],
      onChanged: (v) => onChanged(v ?? 'buy'),
    ),
  );

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const SafeArea(
        child: Center(child: CircularProgressIndicator(color: kGold)),
      );
    }
    return SafeArea(
      child: RefreshIndicator(
        color: kGold,
        onRefresh: _load,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            _PageIntro(
              'Bot Config',
              'Account-specific strategy, schedule, sizing, and alert settings.',
              onRefresh: _load,
            ),
            if (_error != null)
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Text(_error!, style: const TextStyle(color: kRed)),
                ),
              ),

            _section('BOT'),
            Card(
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 6, 16, 10),
                child: Column(
                  children: [
                    _switchTile(
                      'Dry Run',
                      _dryRun,
                      (v) => setState(() => _dryRun = v),
                      subtitle: 'No real orders when enabled',
                    ),
                    _switchTile(
                      'Telegram Alerts',
                      _telegramAlerts,
                      (v) => setState(() => _telegramAlerts = v),
                    ),
                    _switchTile(
                      'Dynamic Lots',
                      _dynamicLots,
                      (v) => setState(() => _dynamicLots = v),
                      subtitle:
                          'At entry, buy min(configured, affordable with balance)',
                    ),
                    _numField('MAX_TRADES_PER_DAY', 'Max trades per day'),
                    _numField('STRIKE_STEP', 'Strike step (\$)'),
                  ],
                ),
              ),
            ),

            _section('🌅 MORNING TRADE'),
            Card(
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 6, 16, 10),
                child: Column(
                  children: [
                    _switchTile(
                      'Enabled',
                      _morningEnabled,
                      (v) => setState(() => _morningEnabled = v),
                    ),
                    _sideField(
                      _morningSide,
                      (v) => setState(() => _morningSide = v),
                    ),
                    _numField('MORNING_LOTS', 'Lots'),
                    _timeField('morning', 'Entry time'),
                    _switchTile(
                      'Scheduled Exit',
                      _morningExitEnabled,
                      (v) => setState(() => _morningExitEnabled = v),
                      subtitle: 'Off = close via TP/SL / settlement only',
                    ),
                    _timeField('morning_exit', 'Exit time'),
                  ],
                ),
              ),
            ),

            _section('🌇 EVENING TRADE'),
            Card(
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 6, 16, 10),
                child: Column(
                  children: [
                    _switchTile(
                      'Enabled',
                      _eveningEnabled,
                      (v) => setState(() => _eveningEnabled = v),
                    ),
                    _sideField(
                      _eveningSide,
                      (v) => setState(() => _eveningSide = v),
                    ),
                    _numField('STRADDLE_LOTS', 'Lots'),
                    _timeField('entry', 'Entry time'),
                    _switchTile(
                      'Scheduled Exit',
                      _eveningExitEnabled,
                      (v) => setState(() => _eveningExitEnabled = v),
                      subtitle: 'Off = close via TP/SL / settlement only',
                    ),
                    _timeField('exit', 'Exit time'),
                  ],
                ),
              ),
            ),

            const SizedBox(height: 20),
            SizedBox(
              width: double.infinity,
              child: FilledButton(
                style: FilledButton.styleFrom(
                  padding: const EdgeInsets.symmetric(vertical: 14),
                ),
                onPressed: _saving ? null : _save,
                child: Text(
                  _saving ? 'Saving…' : 'SAVE ALL CONFIGS',
                  style: const TextStyle(
                    fontWeight: FontWeight.w700,
                    letterSpacing: 1,
                  ),
                ),
              ),
            ),
            const SizedBox(height: 8),
            const Center(
              child: Text(
                'The account bot reloads saved changes automatically within about 30 seconds.',
                style: TextStyle(color: kMuted, fontSize: 11),
              ),
            ),
            const SizedBox(height: 24),
          ],
        ),
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────
// Settings page
// ─────────────────────────────────────────────────────────────
class SettingsPage extends StatefulWidget {
  final VoidCallback? onSignOut;
  const SettingsPage({super.key, this.onSignOut});

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  late final TextEditingController _urlCtl;
  late final TextEditingController _userCtl;
  late final TextEditingController _passCtl;
  String? _testResult;
  bool _testing = false;

  @override
  void initState() {
    super.initState();
    _urlCtl = TextEditingController(text: Api.baseUrl);
    _userCtl = TextEditingController(text: Api.user);
    _passCtl = TextEditingController(text: Api.pass);
  }

  @override
  void dispose() {
    _urlCtl.dispose();
    _userCtl.dispose();
    _passCtl.dispose();
    super.dispose();
  }

  Future<void> _saveAndTest() async {
    setState(() {
      _testing = true;
      _testResult = null;
    });
    await Api.saveBaseUrl(_urlCtl.text, _userCtl.text, _passCtl.text);
    try {
      final d = await Api.getJson('/api/status');
      setState(
        () => _testResult =
            '✅ Connected — evening: ${d['status'] ?? '?'}, morning: ${d['morning']?['status'] ?? '—'}',
      );
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
          const _PageIntro(
            'Settings',
            'Connection, signed-in account, and app information.',
          ),
          Card(
            child: ListTile(
              leading: const CircleAvatar(
                backgroundColor: Color(0xFFEEF3FE),
                foregroundColor: kGold,
                child: Icon(Icons.person),
              ),
              title: Text(
                Api.displayName.isEmpty ? Api.user : Api.displayName,
                style: const TextStyle(fontWeight: FontWeight.w700),
              ),
              subtitle: Text(
                'Signed in as ${Api.user}',
                style: const TextStyle(color: kMuted, fontSize: 12),
              ),
              trailing: TextButton(
                onPressed: widget.onSignOut,
                child: const Text(
                  'Switch account',
                  style: TextStyle(color: kGold),
                ),
              ),
            ),
          ),
          const SizedBox(height: 16),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    'Bot Server URL',
                    style: TextStyle(color: kText, fontWeight: FontWeight.w600),
                  ),
                  const SizedBox(height: 4),
                  const Text(
                    'The AWS dashboard address used by this phone. HTTPS is recommended when a domain is available.',
                    style: TextStyle(color: kMuted, fontSize: 12),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _urlCtl,
                    keyboardType: TextInputType.url,
                    style: const TextStyle(fontFamily: 'monospace'),
                    decoration: InputDecoration(
                      hintText: 'http://13.207.78.56:5001',
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(8),
                      ),
                    ),
                  ),
                  const SizedBox(height: 12),
                  Row(
                    children: [
                      Expanded(
                        child: TextField(
                          controller: _userCtl,
                          decoration: InputDecoration(
                            labelText: 'Username',
                            border: OutlineInputBorder(
                              borderRadius: BorderRadius.circular(8),
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: TextField(
                          controller: _passCtl,
                          obscureText: true,
                          decoration: InputDecoration(
                            labelText: 'Password',
                            border: OutlineInputBorder(
                              borderRadius: BorderRadius.circular(8),
                            ),
                          ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 12),
                  SizedBox(
                    width: double.infinity,
                    child: FilledButton(
                      onPressed: _testing ? null : _saveAndTest,
                      child: Text(
                        _testing ? 'Testing…' : 'Save & Test Connection',
                      ),
                    ),
                  ),
                  if (_testResult != null) ...[
                    const SizedBox(height: 12),
                    Text(
                      _testResult!,
                      style: TextStyle(
                        color: _testResult!.startsWith('✅') ? kGreen : kRed,
                        fontSize: 13,
                      ),
                    ),
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
                  Text(
                    'About',
                    style: TextStyle(color: kText, fontWeight: FontWeight.w600),
                  ),
                  SizedBox(height: 8),
                  Text(
                    'Nithi-bot mobile — MV-BTC daily straddle bot on Delta Exchange India.\n\n'
                    '🌅 Morning trade: 5:45 AM IST (settles 5:30 PM)\n'
                    '🌇 Evening trade: 5:35 PM IST (default exit 1:00 AM)\n\n'
                    'This app is a secure remote control for the bot and dashboard running on AWS.',
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

// ─────────────────────────────────────────────────────────────
// Payoff-at-settlement chart (dependency-free CustomPainter)
// ─────────────────────────────────────────────────────────────
class PayoffPainter extends CustomPainter {
  final List<double> xs, ys;
  final double spot, spotPnl;
  PayoffPainter({
    required this.xs,
    required this.ys,
    required this.spot,
    required this.spotPnl,
  });

  @override
  void paint(Canvas canvas, Size size) {
    if (xs.length < 2) return;
    const padL = 46.0, padR = 8.0, padT = 8.0, padB = 22.0;
    final w = size.width - padL - padR, h = size.height - padT - padB;

    double yMin = ys.reduce((a, b) => a < b ? a : b);
    double yMax = ys.reduce((a, b) => a > b ? a : b);
    if (yMin > 0) yMin = 0;
    if (yMax < 0) yMax = 0;
    final ySpan = (yMax - yMin) == 0 ? 1 : (yMax - yMin);
    final xMin = xs.first, xSpan = xs.last - xs.first;

    double px(double x) => padL + (x - xMin) / xSpan * w;
    double py(double y) => padT + (yMax - y) / ySpan * h;

    final grid = Paint()
      ..color = kBorder
      ..strokeWidth = 1;
    final txt = TextStyle(color: kMuted, fontSize: 9);

    // Horizontal gridlines + y labels (min, 0, max)
    for (final y in {yMin, 0.0, yMax}) {
      canvas.drawLine(
        Offset(padL, py(y)),
        Offset(padL + w, py(y)),
        y == 0
            ? (Paint()
                ..color = kMuted
                ..strokeWidth = 1.2)
            : grid,
      );
      final tp = TextPainter(
        text: TextSpan(
          text:
              '${y < 0
                  ? '-'
                  : y > 0
                  ? '+'
                  : ''}\$${y.abs().round()}',
          style: txt,
        ),
        textDirection: TextDirection.ltr,
      )..layout();
      tp.paint(canvas, Offset(padL - tp.width - 4, py(y) - tp.height / 2));
    }
    // X labels (first, strike-ish middle, last)
    for (final i in [0, xs.length ~/ 2, xs.length - 1]) {
      final tp = TextPainter(
        text: TextSpan(text: '\$${xs[i].round()}', style: txt),
        textDirection: TextDirection.ltr,
      )..layout();
      tp.paint(
        canvas,
        Offset(px(xs[i]) - tp.width / 2, size.height - padB + 6),
      );
    }

    // Payoff polyline, green above zero / red below, per segment
    for (var i = 0; i < xs.length - 1; i++) {
      final paint = Paint()
        ..color = (ys[i] + ys[i + 1]) / 2 >= 0 ? kGreen : kRed
        ..strokeWidth = 2.2
        ..style = PaintingStyle.stroke;
      canvas.drawLine(
        Offset(px(xs[i]), py(ys[i])),
        Offset(px(xs[i + 1]), py(ys[i + 1])),
        paint,
      );
    }

    // Current spot marker
    final sx = px(spot.clamp(xs.first, xs.last)),
        sy = py(spotPnl.clamp(yMin, yMax));
    canvas.drawLine(
      Offset(sx, padT),
      Offset(sx, padT + h),
      Paint()
        ..color = const Color(0x552563EB)
        ..strokeWidth = 1,
    );
    canvas.drawCircle(
      Offset(sx, sy),
      4.5,
      Paint()..color = const Color(0xFF2563EB),
    );
    canvas.drawCircle(
      Offset(sx, sy),
      4.5,
      Paint()
        ..color = Colors.white
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.2,
    );
  }

  @override
  bool shouldRepaint(covariant PayoffPainter old) =>
      old.xs != xs || old.ys != ys || old.spot != spot;
}
