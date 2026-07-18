import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:webview_flutter/webview_flutter.dart';

const kAccent = Color(0xFF2563EB);
const kPositive = Color(0xFF08875D);
const kNegative = Color(0xFFD92D20);
const kWarning = Color(0xFFB45309);

const kLightBackground = Color(0xFFF2F4F8);
const kLightSurface = Color(0xFFFFFFFF);
const kLightBorder = Color(0xFFE4E8F0);
const kLightText = Color(0xFF1A2333);
const kLightMuted = Color(0xFF68758A);

const kDarkBackground = Color(0xFF101722);
const kDarkSurface = Color(0xFF182231);
const kDarkBorder = Color(0xFF2C3A50);
const kDarkText = Color(0xFFEDF3FC);
const kDarkMuted = Color(0xFFA0AFC3);
const kDarkAccent = Color(0xFF70A5FF);

final appTheme = AppThemeController();

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await appTheme.load();
  runApp(const MathiBotApp());
}

class AppThemeController extends ChangeNotifier {
  ThemeMode _mode = ThemeMode.light;

  ThemeMode get mode => _mode;
  bool get isDark => _mode == ThemeMode.dark;

  Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    _mode = prefs.getBool('app_dark_theme') == true
        ? ThemeMode.dark
        : ThemeMode.light;
  }

  Future<void> setDark(bool enabled) async {
    final next = enabled ? ThemeMode.dark : ThemeMode.light;
    if (_mode == next) return;
    _mode = next;
    notifyListeners();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool('app_dark_theme', enabled);
  }
}

ThemeData buildAppTheme({required bool dark}) {
  final background = dark ? kDarkBackground : kLightBackground;
  final surface = dark ? kDarkSurface : kLightSurface;
  final border = dark ? kDarkBorder : kLightBorder;
  final text = dark ? kDarkText : kLightText;
  final muted = dark ? kDarkMuted : kLightMuted;
  final accent = dark ? kDarkAccent : kAccent;

  final scheme = ColorScheme(
    brightness: dark ? Brightness.dark : Brightness.light,
    primary: accent,
    onPrimary: dark ? kDarkBackground : Colors.white,
    secondary: accent,
    onSecondary: dark ? kDarkBackground : Colors.white,
    error: dark ? const Color(0xFFFF7B72) : kNegative,
    onError: dark ? kDarkBackground : Colors.white,
    surface: surface,
    onSurface: text,
  );

  return ThemeData(
    useMaterial3: true,
    brightness: dark ? Brightness.dark : Brightness.light,
    fontFamily: 'Roboto',
    scaffoldBackgroundColor: background,
    colorScheme: scheme,
    dividerColor: border,
    splashFactory: InkSparkle.splashFactory,
    appBarTheme: AppBarTheme(
      backgroundColor: surface,
      foregroundColor: text,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
      scrolledUnderElevation: 0,
      shape: Border(bottom: BorderSide(color: border)),
      titleTextStyle: TextStyle(
        color: text,
        fontSize: 16,
        height: 1.15,
        fontWeight: FontWeight.w700,
        letterSpacing: -.15,
      ),
    ),
    cardTheme: CardThemeData(
      color: surface,
      elevation: 0,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: border),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: surface,
      labelStyle: TextStyle(color: muted, fontSize: 13),
      hintStyle: TextStyle(color: muted, fontSize: 13),
      contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 14),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: BorderSide(color: border),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: BorderSide(color: border),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: BorderSide(color: accent, width: 1.5),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: accent,
        foregroundColor: dark ? kDarkBackground : Colors.white,
        minimumSize: const Size(0, 48),
        textStyle: const TextStyle(fontSize: 14, fontWeight: FontWeight.w700),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
    ),
    navigationBarTheme: NavigationBarThemeData(
      height: 68,
      backgroundColor: surface,
      indicatorColor: accent.withValues(alpha: dark ? .20 : .10),
      surfaceTintColor: Colors.transparent,
      labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
      labelTextStyle: WidgetStateProperty.resolveWith(
        (states) => TextStyle(
          color: states.contains(WidgetState.selected) ? accent : muted,
          fontSize: 9.5,
          fontWeight: states.contains(WidgetState.selected)
              ? FontWeight.w700
              : FontWeight.w600,
        ),
      ),
      iconTheme: WidgetStateProperty.resolveWith(
        (states) => IconThemeData(
          color: states.contains(WidgetState.selected) ? accent : muted,
          size: 21,
        ),
      ),
    ),
    progressIndicatorTheme: ProgressIndicatorThemeData(color: accent),
    switchTheme: SwitchThemeData(
      thumbColor: WidgetStateProperty.resolveWith(
        (states) => states.contains(WidgetState.selected)
            ? (dark ? kDarkBackground : Colors.white)
            : muted,
      ),
      trackColor: WidgetStateProperty.resolveWith(
        (states) => states.contains(WidgetState.selected) ? accent : border,
      ),
    ),
  );
}

class MathiBotApp extends StatelessWidget {
  const MathiBotApp({super.key});

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: appTheme,
      builder: (context, _) => MaterialApp(
        title: 'Nithi Bot',
        debugShowCheckedModeBanner: false,
        theme: buildAppTheme(dark: false),
        darkTheme: buildAppTheme(dark: true),
        themeMode: appTheme.mode,
        home: const HomeShell(),
      ),
    );
  }
}

class AppPageSpec {
  const AppPageSpec({
    required this.label,
    required this.navLabel,
    required this.path,
    required this.icon,
  });

  final String label;
  final String navLabel;
  final String path;
  final IconData icon;
}

const appPages = <AppPageSpec>[
  AppPageSpec(
    label: 'Nithi Bot',
    navLabel: 'Home',
    path: '/',
    icon: Icons.home_outlined,
  ),
  AppPageSpec(
    label: 'Trades & P&L',
    navLabel: 'Trades',
    path: '/trades',
    icon: Icons.trending_up_rounded,
  ),
  AppPageSpec(
    label: 'Dry Run',
    navLabel: 'Dry Run',
    path: '/dry-run',
    icon: Icons.science_outlined,
  ),
  AppPageSpec(
    label: 'Positions',
    navLabel: 'Positions',
    path: '/positions',
    icon: Icons.view_list_outlined,
  ),
  AppPageSpec(
    label: 'Bot Config',
    navLabel: 'Config',
    path: '/config',
    icon: Icons.tune_rounded,
  ),
  AppPageSpec(
    label: 'API Accounts',
    navLabel: 'Accounts',
    path: '/accounts',
    icon: Icons.manage_accounts_outlined,
  ),
];

class SessionService {
  static const _defaultUrl = 'https://mathibot.duckdns.org';

  static String baseUrl = _defaultUrl;
  static String username = 'mathi';
  static String password = '';
  static String displayName = '';
  static final cookieManager = WebViewCookieManager();

  static Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    baseUrl = _normaliseUrl(prefs.getString('server_url') ?? _defaultUrl);
    username = prefs.getString('server_user') ?? 'mathi';
    password = prefs.getString('server_pass') ?? '';
  }

  static Future<void> save({
    required String url,
    required String user,
    required String pass,
  }) async {
    baseUrl = _normaliseUrl(url);
    username = user.trim();
    password = pass;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('server_url', baseUrl);
    await prefs.setString('server_user', username);
    await prefs.setString('server_pass', password);
  }

  static String _normaliseUrl(String value) {
    var result = value.trim();
    if (result.isEmpty) result = _defaultUrl;
    if (!result.startsWith('http://') && !result.startsWith('https://')) {
      result = 'https://$result';
    }
    return result.replaceAll(RegExp(r'/+$'), '');
  }

  static String? sessionCookieFromHeader(String header) {
    return RegExp(
      r'(?:^|[,;]\s*)session=([^;,]+)',
    ).firstMatch(header)?.group(1);
  }

  static Future<void> authenticate({
    String? url,
    String? user,
    String? pass,
    bool persist = true,
  }) async {
    final nextUrl = _normaliseUrl(url ?? baseUrl);
    final nextUser = (user ?? username).trim();
    final nextPass = pass ?? password;
    if (nextUser.isEmpty || nextPass.isEmpty) {
      throw Exception('Username and password are required.');
    }

    final response = await http
        .post(
          Uri.parse('$nextUrl/login'),
          headers: const {'Content-Type': 'application/json'},
          body: jsonEncode({'username': nextUser, 'password': nextPass}),
        )
        .timeout(const Duration(seconds: 20));

    Map<String, dynamic> body = {};
    try {
      body = jsonDecode(response.body) as Map<String, dynamic>;
    } catch (_) {}
    if (response.statusCode != 200 || body['ok'] != true) {
      throw Exception(
        (body['error'] ?? 'Login failed (${response.statusCode})').toString(),
      );
    }

    final setCookie = response.headers['set-cookie'] ?? '';
    final sessionCookie = sessionCookieFromHeader(setCookie);
    if (sessionCookie == null || sessionCookie.isEmpty) {
      throw Exception('The server did not return an authenticated session.');
    }

    await cookieManager.clearCookies();
    final server = Uri.parse(nextUrl);
    await cookieManager.setCookie(
      WebViewCookie(
        name: 'session',
        value: sessionCookie,
        domain: server.host,
        path: '/',
      ),
    );

    baseUrl = nextUrl;
    username = nextUser;
    password = nextPass;
    displayName = (body['display_name'] ?? nextUser).toString();
    if (persist) {
      await save(url: nextUrl, user: nextUser, pass: nextPass);
    }
  }

  static Future<void> signOut() async {
    await cookieManager.clearCookies();
    displayName = '';
    password = '';
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('server_pass');
  }
}

class HomeShell extends StatefulWidget {
  const HomeShell({super.key});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  final _webKeys = List.generate(
    appPages.length,
    (_) => GlobalKey<DashboardWebPageState>(),
  );

  final Set<int> _visitedTabs = {0};
  int _tab = 0;
  bool _ready = false;
  bool _authenticated = false;
  String? _startupError;

  @override
  void initState() {
    super.initState();
    unawaited(_bootstrap());
  }

  Future<void> _bootstrap() async {
    await SessionService.load();
    if (SessionService.password.isNotEmpty) {
      try {
        await SessionService.authenticate(persist: false);
        _authenticated = true;
      } catch (error) {
        _startupError = error.toString().replaceFirst('Exception: ', '');
      }
    }
    if (!mounted) return;
    setState(() => _ready = true);
  }

  Future<void> _signOut() async {
    await SessionService.signOut();
    if (!mounted) return;
    setState(() {
      _authenticated = false;
      _startupError = null;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (!_ready) {
      return const _StartupScreen();
    }
    if (!_authenticated) {
      return LoginScreen(
        initialError: _startupError,
        onSuccess: () => setState(() {
          _authenticated = true;
          _startupError = null;
        }),
      );
    }

    final dark = Theme.of(context).brightness == Brightness.dark;
    final colors = Theme.of(context).colorScheme;
    return Scaffold(
      appBar: AppBar(
        toolbarHeight: 62,
        leadingWidth: 58,
        leading: Padding(
          padding: const EdgeInsets.fromLTRB(14, 10, 6, 10),
          child: DecoratedBox(
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(10),
              boxShadow: [
                BoxShadow(
                  color: Colors.black.withValues(alpha: dark ? .28 : .10),
                  blurRadius: 10,
                  offset: const Offset(0, 2),
                ),
              ],
            ),
            child: ClipRRect(
              borderRadius: BorderRadius.circular(10),
              child: Image.asset('assets/logo.png', fit: BoxFit.cover),
            ),
          ),
        ),
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text('Nithi Bot'),
            const SizedBox(height: 2),
            Text(
              SessionService.displayName.isEmpty
                  ? SessionService.username
                  : SessionService.displayName,
              style: TextStyle(
                color: dark ? kDarkMuted : kLightMuted,
                fontSize: 10.5,
                fontWeight: FontWeight.w500,
              ),
            ),
          ],
        ),
        actions: [
          IconButton(
            tooltip: 'Refresh this tab',
            onPressed: () => _webKeys[_tab].currentState?.reload(),
            icon: const Icon(Icons.refresh_rounded, size: 21),
          ),
          Semantics(
            label: dark ? 'Switch to light theme' : 'Switch to dark theme',
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(
                  dark ? Icons.dark_mode_outlined : Icons.light_mode_outlined,
                  size: 18,
                  color: colors.primary,
                ),
                Transform.scale(
                  scale: .78,
                  child: Switch.adaptive(
                    value: dark,
                    onChanged: appTheme.setDark,
                  ),
                ),
              ],
            ),
          ),
          PopupMenuButton<String>(
            tooltip: 'Account',
            onSelected: (value) {
              if (value == 'logout') unawaited(_signOut());
            },
            itemBuilder: (context) => [
              PopupMenuItem(
                enabled: false,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      SessionService.displayName.isEmpty
                          ? SessionService.username
                          : SessionService.displayName,
                      style: const TextStyle(fontWeight: FontWeight.w700),
                    ),
                    Text(
                      SessionService.baseUrl,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: dark ? kDarkMuted : kLightMuted,
                        fontSize: 11,
                      ),
                    ),
                  ],
                ),
              ),
              const PopupMenuDivider(),
              const PopupMenuItem(
                value: 'logout',
                child: Row(
                  children: [
                    Icon(Icons.logout_rounded, size: 18),
                    SizedBox(width: 10),
                    Text('Sign out'),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(width: 4),
        ],
      ),
      body: IndexedStack(
        index: _tab,
        children: [
          for (var index = 0; index < appPages.length; index++)
            if (_visitedTabs.contains(index))
              DashboardWebPage(
                key: _webKeys[index],
                page: appPages[index],
                dark: dark,
                onSessionExpired: _signOut,
              )
            else
              const SizedBox.shrink(),
        ],
      ),
      bottomNavigationBar: DecoratedBox(
        decoration: BoxDecoration(
          border: Border(
            top: BorderSide(color: Theme.of(context).dividerColor),
          ),
        ),
        child: NavigationBar(
          selectedIndex: _tab,
          onDestinationSelected: (index) => setState(() {
            _tab = index;
            _visitedTabs.add(index);
          }),
          destinations: [
            for (final page in appPages)
              NavigationDestination(
                icon: Icon(page.icon),
                selectedIcon: Icon(page.icon),
                label: page.navLabel,
              ),
          ],
        ),
      ),
    );
  }
}

class _StartupScreen extends StatelessWidget {
  const _StartupScreen();

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ClipRRect(
              borderRadius: BorderRadius.circular(16),
              child: Image.asset('assets/logo.png', width: 72, height: 72),
            ),
            const SizedBox(height: 20),
            const SizedBox(
              width: 28,
              height: 28,
              child: CircularProgressIndicator(strokeWidth: 2.5),
            ),
            const SizedBox(height: 12),
            Text(
              'Preparing Nithi Bot…',
              style: TextStyle(
                color: Theme.of(context).colorScheme.onSurfaceVariant,
                fontSize: 12,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key, required this.onSuccess, this.initialError});

  final VoidCallback onSuccess;
  final String? initialError;

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  late final TextEditingController _url = TextEditingController(
    text: SessionService.baseUrl,
  );
  late final TextEditingController _username = TextEditingController(
    text: SessionService.username,
  );
  late final TextEditingController _password = TextEditingController();

  late String? _error = widget.initialError;
  bool _busy = false;
  bool _obscure = true;

  @override
  void dispose() {
    _url.dispose();
    _username.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _login() async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await SessionService.authenticate(
        url: _url.text,
        user: _username.text,
        pass: _password.text,
      );
      if (mounted) widget.onSuccess();
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = error.toString().replaceFirst('Exception: ', ''));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final dark = Theme.of(context).brightness == Brightness.dark;
    final colors = Theme.of(context).colorScheme;
    return Scaffold(
      body: SafeArea(
        child: Stack(
          children: [
            Positioned(
              top: 8,
              right: 10,
              child: Row(
                children: [
                  Icon(
                    dark ? Icons.dark_mode_outlined : Icons.light_mode_outlined,
                    size: 18,
                    color: colors.primary,
                  ),
                  Transform.scale(
                    scale: .82,
                    child: Switch.adaptive(
                      value: dark,
                      onChanged: appTheme.setDark,
                    ),
                  ),
                ],
              ),
            ),
            Center(
              child: SingleChildScrollView(
                padding: const EdgeInsets.fromLTRB(20, 72, 20, 28),
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 430),
                  child: Card(
                    child: Padding(
                      padding: const EdgeInsets.fromLTRB(24, 26, 24, 24),
                      child: AutofillGroup(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            Align(
                              child: ClipRRect(
                                borderRadius: BorderRadius.circular(16),
                                child: Image.asset(
                                  'assets/logo.png',
                                  width: 76,
                                  height: 76,
                                ),
                              ),
                            ),
                            const SizedBox(height: 18),
                            Text(
                              'Nithi Bot',
                              textAlign: TextAlign.center,
                              style: TextStyle(
                                color: colors.onSurface,
                                fontSize: 24,
                                fontWeight: FontWeight.w800,
                                letterSpacing: -.35,
                              ),
                            ),
                            const SizedBox(height: 4),
                            Text(
                              'Secure access to your trading dashboard',
                              textAlign: TextAlign.center,
                              style: TextStyle(
                                color: colors.onSurfaceVariant,
                                fontSize: 12.5,
                              ),
                            ),
                            const SizedBox(height: 24),
                            TextField(
                              controller: _url,
                              keyboardType: TextInputType.url,
                              autocorrect: false,
                              decoration: const InputDecoration(
                                labelText: 'Server',
                                prefixIcon: Icon(Icons.dns_outlined, size: 20),
                              ),
                            ),
                            const SizedBox(height: 12),
                            TextField(
                              controller: _username,
                              autocorrect: false,
                              autofillHints: const [AutofillHints.username],
                              decoration: const InputDecoration(
                                labelText: 'Username',
                                prefixIcon: Icon(
                                  Icons.person_outline_rounded,
                                  size: 20,
                                ),
                              ),
                            ),
                            const SizedBox(height: 12),
                            TextField(
                              controller: _password,
                              obscureText: _obscure,
                              autofillHints: const [AutofillHints.password],
                              onSubmitted: (_) => _login(),
                              decoration: InputDecoration(
                                labelText: 'Password',
                                prefixIcon: const Icon(
                                  Icons.lock_outline_rounded,
                                  size: 20,
                                ),
                                suffixIcon: IconButton(
                                  tooltip: _obscure
                                      ? 'Show password'
                                      : 'Hide password',
                                  onPressed: () =>
                                      setState(() => _obscure = !_obscure),
                                  icon: Icon(
                                    _obscure
                                        ? Icons.visibility_outlined
                                        : Icons.visibility_off_outlined,
                                    size: 20,
                                  ),
                                ),
                              ),
                            ),
                            if (_error != null) ...[
                              const SizedBox(height: 14),
                              Container(
                                padding: const EdgeInsets.all(11),
                                decoration: BoxDecoration(
                                  color: colors.error.withValues(alpha: .10),
                                  borderRadius: BorderRadius.circular(9),
                                  border: Border.all(
                                    color: colors.error.withValues(alpha: .28),
                                  ),
                                ),
                                child: Row(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Icon(
                                      Icons.error_outline_rounded,
                                      color: colors.error,
                                      size: 18,
                                    ),
                                    const SizedBox(width: 8),
                                    Expanded(
                                      child: Text(
                                        _error!,
                                        style: TextStyle(
                                          color: colors.error,
                                          fontSize: 12,
                                        ),
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                            ],
                            const SizedBox(height: 18),
                            FilledButton.icon(
                              onPressed: _busy ? null : _login,
                              icon: _busy
                                  ? const SizedBox(
                                      width: 18,
                                      height: 18,
                                      child: CircularProgressIndicator(
                                        strokeWidth: 2,
                                        color: Colors.white,
                                      ),
                                    )
                                  : const Icon(Icons.login_rounded, size: 19),
                              label: Text(_busy ? 'Signing in…' : 'Sign in'),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class DashboardWebPage extends StatefulWidget {
  const DashboardWebPage({
    super.key,
    required this.page,
    required this.dark,
    required this.onSessionExpired,
  });

  final AppPageSpec page;
  final bool dark;
  final Future<void> Function() onSessionExpired;

  @override
  State<DashboardWebPage> createState() => DashboardWebPageState();
}

class DashboardWebPageState extends State<DashboardWebPage>
    with AutomaticKeepAliveClientMixin {
  late final WebViewController _controller;
  int _progress = 0;
  String? _error;
  bool _sessionExpiryHandled = false;

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    super.initState();
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setVerticalScrollBarEnabled(true)
      ..setBackgroundColor(widget.dark ? kDarkBackground : kLightBackground)
      ..setNavigationDelegate(
        NavigationDelegate(
          onProgress: (progress) {
            if (mounted) setState(() => _progress = progress);
          },
          onPageStarted: (_) {
            if (mounted) {
              setState(() {
                _progress = 0;
                _error = null;
              });
            }
          },
          onPageFinished: (url) {
            if (mounted) setState(() => _progress = 100);
            unawaited(_applyNativePresentation());
            final uri = Uri.tryParse(url);
            if (uri?.path == '/login' && !_sessionExpiryHandled) {
              _sessionExpiryHandled = true;
              unawaited(widget.onSessionExpired());
            }
          },
          onWebResourceError: (error) {
            if (error.isForMainFrame == true && mounted) {
              setState(() => _error = error.description);
            }
          },
        ),
      );
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant DashboardWebPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.dark != widget.dark) {
      unawaited(
        _controller.setBackgroundColor(
          widget.dark ? kDarkBackground : kLightBackground,
        ),
      );
      unawaited(_applyNativePresentation());
    }
    if (oldWidget.page.path != widget.page.path) unawaited(_load());
  }

  Uri get _pageUri {
    final base = Uri.parse(SessionService.baseUrl);
    return base
        .resolve(widget.page.path)
        .replace(
          queryParameters: {
            'app': '1',
            'theme': widget.dark ? 'dark' : 'light',
          },
        );
  }

  Future<void> _load() => _controller.loadRequest(_pageUri);

  Future<void> reload() async {
    setState(() {
      _error = null;
      _progress = 0;
    });
    await _controller.reload();
  }

  Future<void> _applyNativePresentation() async {
    final theme = widget.dark ? 'dark' : 'light';
    final background = widget.dark ? '#101722' : '#f2f4f8';
    try {
      await _controller.runJavaScript('''
        (() => {
          document.body && document.body.classList.add('native-app');
          if ('$theme' === 'dark') {
            document.documentElement.dataset.theme = 'dark';
          } else {
            delete document.documentElement.dataset.theme;
          }
          localStorage.setItem('nithi-theme', '$theme');
          let meta = document.querySelector('meta[name="theme-color"]');
          if (!meta) {
            meta = document.createElement('meta');
            meta.name = 'theme-color';
            document.head.appendChild(meta);
          }
          meta.content = '$background';
        })();
      ''');
    } catch (_) {
      // A page may be replaced while a theme update is in flight.
    }
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    final colors = Theme.of(context).colorScheme;
    return ColoredBox(
      color: Theme.of(context).scaffoldBackgroundColor,
      child: Stack(
        children: [
          Positioned.fill(child: WebViewWidget(controller: _controller)),
          if (_progress < 100)
            Align(
              alignment: Alignment.topCenter,
              child: LinearProgressIndicator(
                value: _progress <= 0 ? null : _progress / 100,
                minHeight: 2.5,
              ),
            ),
          if (_error != null)
            Positioned.fill(
              child: ColoredBox(
                color: Theme.of(context).scaffoldBackgroundColor,
                child: Center(
                  child: Padding(
                    padding: const EdgeInsets.all(28),
                    child: ConstrainedBox(
                      constraints: const BoxConstraints(maxWidth: 380),
                      child: Card(
                        child: Padding(
                          padding: const EdgeInsets.all(22),
                          child: Column(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              Icon(
                                Icons.cloud_off_rounded,
                                color: colors.error,
                                size: 34,
                              ),
                              const SizedBox(height: 12),
                              Text(
                                'Could not load ${widget.page.label}',
                                textAlign: TextAlign.center,
                                style: const TextStyle(
                                  fontSize: 16,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                              const SizedBox(height: 7),
                              Text(
                                _error!,
                                textAlign: TextAlign.center,
                                style: TextStyle(
                                  color: colors.onSurfaceVariant,
                                  fontSize: 12,
                                ),
                              ),
                              const SizedBox(height: 16),
                              FilledButton.icon(
                                onPressed: reload,
                                icon: const Icon(Icons.refresh_rounded),
                                label: const Text('Try again'),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }
}
