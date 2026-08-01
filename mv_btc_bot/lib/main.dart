import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:webview_flutter/webview_flutter.dart';

import 'api/client.dart';
import 'screens/exposure_screen.dart';
import 'screens/performance_screen.dart';
import 'screens/accounts_screen.dart';
import 'screens/config_screen.dart';
import 'screens/logs_screen.dart';
import 'screens/paper_screen.dart';
import 'screens/today_screen.dart';
import 'screens/trend_engine_screen.dart';

const kPositive = Color(0xFF38D99A);
const kWarning = Color(0xFFFFC267);

const kRedBackground = Color(0xFF0D0608);
const kRedSurface = Color(0xFF180B0E);
const kRedSubtle = Color(0xFF240F13);
const kRedBorder = Color(0x38FF5B6C);
const kRedText = Color(0xFFFFF5F6);
const kRedMuted = Color(0xFFC4A8AD);
const kRedAccent = Color(0xFFFF2F4B);
const kRedAccentBright = Color(0xFFFF7A8D);
const kRedNegative = Color(0xFFFF6172);

const kBlueBackground = Color(0xFF030914);
const kBlueSurface = Color(0xFF071223);
const kBlueSubtle = Color(0xFF081D39);
const kBlueBorder = Color(0x3B5BB4FF);
const kBlueText = Color(0xFFF3F9FF);
const kBlueMuted = Color(0xFFA7BDD6);
const kBlueAccent = Color(0xFF39A7FF);
const kBlueAccentBright = Color(0xFF8BD0FF);
const kBlueNegative = Color(0xFFFF7180);

// Neon green brand accent, mirroring --neon (#39ff14) in static/css/app.css.
// Shared by both native themes on purpose: the web keeps the glow out of its
// per-theme blocks so it reads identically in Red and Blue, and the app now
// does the same for its tab icons and the header brand lock-up.
const kNeon = Color(0xFF39FF14);
const kNeonTitle = Color(0xFFEAFFE4); // .brand .name
const kNeonSubtle = Color(0xFF6DFF4D); // .brand .sub

// The glow is layered rather than one wide blur: a tight bright core keeps the
// glyphs and icon strokes legible, and the wider faint halos do the actual
// neon work. A single large shadow just looks smeared.
const kNeonTextGlow = <Shadow>[
  Shadow(color: Color(0xF239FF14), blurRadius: 4),
  Shadow(color: Color(0xB339FF14), blurRadius: 11),
  Shadow(color: Color(0x7339FF14), blurRadius: 24),
];
const kNeonIconGlow = <Shadow>[
  Shadow(color: Color(0xD939FF14), blurRadius: 4),
  Shadow(color: Color(0x7339FF14), blurRadius: 10),
];
const kNeonIconGlowStrong = <Shadow>[
  Shadow(color: Color(0xF239FF14), blurRadius: 4),
  Shadow(color: Color(0xA639FF14), blurRadius: 14),
  Shadow(color: Color(0x5939FF14), blurRadius: 30),
];

const kRedBackgroundAsset = 'assets/crimson-dashboard-bg.png';
const kBlueBackgroundAsset = 'assets/sparkling-blue-dashboard-bg.png';

final appTheme = AppThemeController();

const kWebAssetRevision = '5.1.0+21-native-exposure-performance';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  ErrorWidget.builder = (_) => const _AppErrorFallback();
  await appTheme.load();
  runApp(const MathiBotApp());
}

/// A release build must never turn a recoverable widget error into an empty
/// page. Detailed diagnostics still go to Flutter's error pipeline; users get
/// a concise, branded recovery message instead of a blank body.
class _AppErrorFallback extends StatelessWidget {
  const _AppErrorFallback();

  @override
  Widget build(BuildContext context) {
    return const Directionality(
      textDirection: TextDirection.ltr,
      child: ColoredBox(
        color: kBlueBackground,
        child: Center(
          child: Padding(
            padding: EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(Icons.sync_problem_rounded, color: kWarning, size: 36),
                SizedBox(height: 12),
                Text(
                  'This screen could not be displayed',
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    color: kBlueText,
                    fontSize: 18,
                    fontWeight: FontWeight.w800,
                  ),
                ),
                SizedBox(height: 6),
                Text(
                  'Reopen the page to try again.',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: kBlueMuted, fontSize: 13),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class AppThemeController extends ChangeNotifier {
  static const _preferenceKey = 'app_blue_theme';
  static const _legacyPreferenceKey = 'app_dark_theme';

  bool _isBlue = false;

  bool get isBlue => _isBlue;

  Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getBool(_preferenceKey);
    final legacy = prefs.getBool(_legacyPreferenceKey);
    _isBlue = saved ?? legacy ?? false;
    if (saved == null && legacy != null) {
      await prefs.setBool(_preferenceKey, legacy);
    }
  }

  Future<void> setBlue(bool enabled) async {
    if (_isBlue == enabled) return;
    _isBlue = enabled;
    notifyListeners();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(_preferenceKey, enabled);
  }
}

ThemeData buildAppTheme({required bool blue}) {
  final background = blue ? kBlueBackground : kRedBackground;
  final surface = blue ? kBlueSurface : kRedSurface;
  final subtle = blue ? kBlueSubtle : kRedSubtle;
  final border = blue ? kBlueBorder : kRedBorder;
  final text = blue ? kBlueText : kRedText;
  final muted = blue ? kBlueMuted : kRedMuted;
  final accent = blue ? kBlueAccent : kRedAccent;
  final accentBright = blue ? kBlueAccentBright : kRedAccentBright;
  final negative = blue ? kBlueNegative : kRedNegative;

  final scheme =
      ColorScheme(
        brightness: Brightness.dark,
        primary: accent,
        onPrimary: Colors.white,
        secondary: accentBright,
        onSecondary: background,
        error: negative,
        onError: background,
        surface: surface,
        onSurface: text,
      ).copyWith(
        onSurfaceVariant: muted,
        outline: border,
        outlineVariant: border,
        surfaceContainerHighest: subtle,
      );

  return ThemeData(
    useMaterial3: true,
    brightness: Brightness.dark,
    fontFamily: 'Roboto',
    scaffoldBackgroundColor: Colors.transparent,
    canvasColor: surface,
    colorScheme: scheme,
    dividerColor: border,
    splashFactory: InkSparkle.splashFactory,
    appBarTheme: AppBarTheme(
      backgroundColor: surface.withValues(alpha: .91),
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
      color: surface.withValues(alpha: .88),
      elevation: 0,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: border),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: subtle.withValues(alpha: .86),
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
        foregroundColor: Colors.white,
        minimumSize: const Size(0, 48),
        textStyle: const TextStyle(fontSize: 14, fontWeight: FontWeight.w700),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
    ),
    navigationBarTheme: NavigationBarThemeData(
      height: 70,
      backgroundColor: surface.withValues(alpha: .94),
      indicatorColor: kNeon.withValues(alpha: .16),
      surfaceTintColor: Colors.transparent,
      labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
      // The label keeps following each theme's accent/muted colours, exactly
      // like `.nav a` on the web: only the icon goes neon, so the selected tab
      // still reads as Red or Blue.
      labelTextStyle: WidgetStateProperty.resolveWith(
        (states) => TextStyle(
          color: states.contains(WidgetState.selected) ? accent : muted,
          fontSize: 9,
          fontWeight: states.contains(WidgetState.selected)
              ? FontWeight.w700
              : FontWeight.w600,
        ),
      ),
      // Neon in both states, brighter when selected — `.nav a svg` versus
      // `.nav a.active svg`. The bottom bar has no hover to lean on, so the
      // idle icon is also held slightly back in opacity; together with the
      // indicator pill that keeps the selected tab obvious.
      iconTheme: WidgetStateProperty.resolveWith(
        (states) => states.contains(WidgetState.selected)
            ? const IconThemeData(
                color: kNeon,
                size: 21,
                shadows: kNeonIconGlowStrong,
              )
            : IconThemeData(
                color: kNeon.withValues(alpha: .72),
                size: 21,
                shadows: kNeonIconGlow,
              ),
      ),
    ),
    progressIndicatorTheme: ProgressIndicatorThemeData(color: accent),
    switchTheme: SwitchThemeData(
      thumbColor: WidgetStateProperty.resolveWith(
        (states) =>
            states.contains(WidgetState.selected) ? kBlueText : kRedText,
      ),
      trackColor: WidgetStateProperty.resolveWith(
        (states) =>
            states.contains(WidgetState.selected) ? kBlueAccent : kRedAccent,
      ),
    ),
    popupMenuTheme: PopupMenuThemeData(
      color: surface.withValues(alpha: .98),
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: border),
      ),
    ),
  );
}

class ThemeBackdrop extends StatelessWidget {
  const ThemeBackdrop({super.key, required this.blue, required this.child});

  final bool blue;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final background = blue ? kBlueBackground : kRedBackground;
    final image = blue ? kBlueBackgroundAsset : kRedBackgroundAsset;
    final systemOverlay = SystemUiOverlayStyle.light.copyWith(
      statusBarColor: Colors.transparent,
      systemNavigationBarColor: background,
      systemNavigationBarIconBrightness: Brightness.light,
    );
    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: systemOverlay,
      child: ColoredBox(
        color: background,
        child: Stack(
          fit: StackFit.expand,
          children: [
            ExcludeSemantics(
              child: Image.asset(
                image,
                fit: BoxFit.cover,
                alignment: Alignment.bottomCenter,
                filterQuality: FilterQuality.medium,
              ),
            ),
            DecoratedBox(
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topCenter,
                  end: Alignment.bottomCenter,
                  colors: blue
                      ? const [Color(0x2902060D), Color(0xA3030914)]
                      : const [Color(0x2E080406), Color(0xA60D0608)],
                ),
              ),
            ),
            child,
          ],
        ),
      ),
    );
  }
}

class RedBlueThemeToggle extends StatelessWidget {
  const RedBlueThemeToggle({super.key, this.compact = false});

  final bool compact;

  @override
  Widget build(BuildContext context) {
    final blue = appTheme.isBlue;
    final message = blue ? 'Switch to Red theme' : 'Switch to Blue theme';
    return Semantics(
      label: message,
      toggled: blue,
      child: Tooltip(
        message: message,
        child: Container(
          height: compact ? 30 : 34,
          padding: EdgeInsets.symmetric(horizontal: compact ? 5 : 7),
          decoration: BoxDecoration(
            color: Theme.of(context).colorScheme.surface.withValues(alpha: .78),
            borderRadius: BorderRadius.circular(999),
            border: Border.all(color: Theme.of(context).dividerColor),
            boxShadow: [
              BoxShadow(
                color: Theme.of(
                  context,
                ).colorScheme.primary.withValues(alpha: .12),
                blurRadius: 12,
              ),
            ],
          ),
          child: ExcludeSemantics(
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  'RED',
                  style: TextStyle(
                    color: blue
                        ? kRedAccentBright.withValues(alpha: .58)
                        : kRedAccentBright,
                    fontSize: compact ? 8 : 9,
                    fontWeight: FontWeight.w900,
                    letterSpacing: .35,
                  ),
                ),
                SizedBox(
                  width: compact ? 30 : 34,
                  height: compact ? 23 : 26,
                  child: FittedBox(
                    fit: BoxFit.fill,
                    child: Switch.adaptive(
                      value: blue,
                      onChanged: appTheme.setBlue,
                    ),
                  ),
                ),
                Text(
                  'BLUE',
                  style: TextStyle(
                    color: blue
                        ? kBlueAccentBright
                        : kBlueAccentBright.withValues(alpha: .58),
                    fontSize: compact ? 8 : 9,
                    fontWeight: FontWeight.w900,
                    letterSpacing: .25,
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

/// The logo with the web's `.brand img.logo` treatment: a thin neon edge, a
/// hard unblurred ring so the corner stays crisp instead of dissolving into
/// its own halo, then widening blurred halos.
class NeonLogo extends StatelessWidget {
  const NeonLogo({super.key, required this.size, required this.radius});

  final double size;
  final double radius;

  @override
  Widget build(BuildContext context) {
    final corner = BorderRadius.circular(radius);
    return SizedBox(
      width: size,
      height: size,
      child: DecoratedBox(
        decoration: BoxDecoration(
          borderRadius: corner,
          border: Border.all(color: const Color(0x8C39FF14)),
          boxShadow: const [
            BoxShadow(color: Color(0x1A39FF14), spreadRadius: 3),
            BoxShadow(color: Color(0x9939FF14), blurRadius: 8),
            BoxShadow(color: Color(0x6639FF14), blurRadius: 18),
            BoxShadow(color: Color(0x3339FF14), blurRadius: 34),
          ],
        ),
        child: ClipRRect(
          borderRadius: corner,
          child: Image.asset('assets/logo.png', fit: BoxFit.cover),
        ),
      ),
    );
  }
}

/// `.brand .name` — neon-tinted off-white over the layered glow. The tracking
/// is the web's .06em, expressed against whichever size the caller needs.
TextStyle neonBrandTextStyle({required double fontSize}) => TextStyle(
  color: kNeonTitle,
  fontSize: fontSize,
  fontWeight: FontWeight.w700,
  letterSpacing: fontSize * .06,
  shadows: kNeonTextGlow,
);

class MathiBotApp extends StatelessWidget {
  const MathiBotApp({super.key});

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: appTheme,
      builder: (context, _) => MaterialApp(
        title: 'Nithi Bot',
        debugShowCheckedModeBanner: false,
        theme: buildAppTheme(blue: appTheme.isBlue),
        themeAnimationDuration: const Duration(milliseconds: 220),
        builder: (context, child) => ThemeBackdrop(
          blue: appTheme.isBlue,
          child: child ?? const SizedBox.shrink(),
        ),
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

// Order matches the web sidebar's primary_items (templates/base.html):
// operational pages first, Trend Engine last — it's a separate section
// there too, just without the nav-gap spacer this flat list has no room for.
const appPages = <AppPageSpec>[
  AppPageSpec(
    label: 'Today',
    navLabel: 'Today',
    path: '/',
    icon: Icons.home_outlined,
  ),
  AppPageSpec(
    label: 'Performance',
    // 'Performance' is the widest tab in the bar and the only one that has to
    // shrink to fit; 'Trades' matches the /trades route it opens.
    navLabel: 'Trades',
    path: '/trades',
    icon: Icons.trending_up_rounded,
  ),
  AppPageSpec(
    label: 'Paper',
    navLabel: 'Paper',
    path: '/dry-run',
    icon: Icons.science_outlined,
  ),
  AppPageSpec(
    label: 'Exposure',
    navLabel: 'Exposure',
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
  AppPageSpec(
    label: 'Logs',
    navLabel: 'Logs',
    path: '/logs',
    icon: Icons.receipt_long_outlined,
  ),
  AppPageSpec(
    label: 'Trend Engine',
    navLabel: 'Trend',
    path: '/trend-engine',
    icon: Icons.insights_rounded,
  ),
];

/// The high-frequency phone tabs. Other native pages live in the More sheet;
/// tablets expose all destinations in a NavigationRail.
const primaryPageIndexes = <int>[0, 1, 2, 7];

class SessionService {
  static const _defaultUrl = 'https://mathibot.duckdns.org';

  static String baseUrl = _defaultUrl;
  static String username = 'mathi';
  static String password = '';
  static String displayName = '';

  /// The Flask session cookie from the last successful sign-in.
  ///
  /// Retained so the native screens' JSON calls can authenticate as the same
  /// session the WebView uses. Previously it was handed to the cookie manager
  /// and dropped, which left `http` requests anonymous — they came back 401
  /// and a native screen would have read that as "no data" rather than "not
  /// signed in". Cleared on sign-out with everything else.
  static String? sessionCookie;

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
    final cookie = sessionCookieFromHeader(setCookie);
    if (cookie == null || cookie.isEmpty) {
      throw Exception('The server did not return an authenticated session.');
    }
    sessionCookie = cookie;

    await cookieManager.clearCookies();
    final server = Uri.parse(nextUrl);
    await cookieManager.setCookie(
      WebViewCookie(
        name: 'session',
        value: cookie,
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
    // Must be cleared with the rest: a retained cookie would let the native
    // screens keep fetching and rendering account data after sign-out.
    sessionCookie = null;
    displayName = '';
    password = '';
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('server_pass');
  }
}

class WebAssetCache {
  static const _preferenceKey = 'web_asset_revision';
  static Future<void>? _preparation;

  static Future<void> prepare(WebViewController controller) {
    return _preparation ??= _prepare(controller);
  }

  static Future<void> _prepare(WebViewController controller) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      if (prefs.getString(_preferenceKey) == kWebAssetRevision) return;
      // Existing installations retain Android WebView cache across APK
      // upgrades. Clear only HTTP assets once for this release so the latest
      // Red/Blue artwork and Trend Engine UI replace the previous page.
      // Cookies and local storage remain untouched.
      await controller.clearCache();
      await prefs.setString(_preferenceKey, kWebAssetRevision);
    } catch (_) {
      // Cache maintenance must never prevent the authenticated dashboard
      // itself from loading.
    }
  }
}

class HomeShell extends StatefulWidget {
  const HomeShell({super.key});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
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

  /// Every page is native. The web dashboard remains the server/API surface,
  /// not a visual dependency of the Android app.
  Widget _pageBody(int index, bool blue) {
    final page = appPages[index];
    final api = DashboardApi(
      baseUrl: SessionService.baseUrl,
      sessionCookie: SessionService.sessionCookie,
    );
    return switch (page.path) {
      '/' => TodayScreen(api: api, onUnauthorised: _signOut),
      '/positions' => ExposureScreen(api: api, onUnauthorised: _signOut),
      '/trades' => PerformanceScreen(api: api, onUnauthorised: _signOut),
      '/dry-run' => PaperScreen(api: api, onUnauthorised: _signOut),
      '/config' => ConfigScreen(
        api: api,
        onUnauthorised: _signOut,
        displayName: SessionService.displayName.isEmpty
            ? SessionService.username
            : SessionService.displayName,
      ),
      '/accounts' => AccountsScreen(api: api, onUnauthorised: _signOut),
      '/logs' => LogsScreen(api: api, onUnauthorised: _signOut),
      '/trend-engine' => TrendEngineScreen(api: api, onUnauthorised: _signOut),
      _ => const SizedBox.shrink(),
    };
  }

  void _selectTab(int index) {
    if (index < 0 || index >= appPages.length) return;
    setState(() {
      _tab = index;
      _visitedTabs.add(index);
    });
  }

  Future<void> _showMore() async {
    const secondary = [3, 4, 5, 6];
    final selected = await showModalBottomSheet<int>(
      context: context,
      useSafeArea: true,
      backgroundColor: Theme.of(context).colorScheme.surface,
      builder: (context) => Padding(
        padding: const EdgeInsets.fromLTRB(18, 18, 18, 24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Expanded(
                  child: Text(
                    'Workspace',
                    style: TextStyle(fontSize: 17, fontWeight: FontWeight.w800),
                  ),
                ),
                IconButton(
                  onPressed: () => Navigator.pop(context),
                  icon: const Icon(Icons.close_rounded),
                ),
              ],
            ),
            const SizedBox(height: 10),
            GridView.count(
              crossAxisCount: 2,
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              mainAxisSpacing: 10,
              crossAxisSpacing: 10,
              childAspectRatio: 2.25,
              children: [
                for (final index in secondary)
                  InkWell(
                    borderRadius: BorderRadius.circular(14),
                    onTap: () => Navigator.pop(context, index),
                    child: Ink(
                      decoration: BoxDecoration(
                        gradient: LinearGradient(
                          colors: [
                            Theme.of(
                              context,
                            ).colorScheme.primary.withValues(alpha: .18),
                            Theme.of(context)
                                .colorScheme
                                .surfaceContainerHighest
                                .withValues(alpha: .7),
                          ],
                        ),
                        borderRadius: BorderRadius.circular(14),
                        border: Border.all(
                          color: Theme.of(context).colorScheme.outline,
                        ),
                      ),
                      child: Row(
                        children: [
                          const SizedBox(width: 14),
                          Icon(
                            appPages[index].icon,
                            color: kNeon,
                            size: 21,
                            shadows: kNeonIconGlow,
                          ),
                          const SizedBox(width: 10),
                          Expanded(
                            child: Text(
                              appPages[index].label,
                              style: const TextStyle(
                                fontSize: 12,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
    if (selected != null) _selectTab(selected);
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

    final blue = appTheme.isBlue;
    final muted = blue ? kBlueMuted : kRedMuted;
    final wide = MediaQuery.sizeOf(context).width >= 840;
    final primaryIndex = primaryPageIndexes.indexOf(_tab);
    final pageStack = IndexedStack(
      index: _tab,
      children: [
        for (var index = 0; index < appPages.length; index++)
          if (_visitedTabs.contains(index))
            _pageBody(index, blue)
          else
            const SizedBox.shrink(),
      ],
    );
    return Scaffold(
      appBar: AppBar(
        toolbarHeight: 62,
        leadingWidth: 58,
        leading: const Padding(
          padding: EdgeInsets.fromLTRB(14, 10, 6, 10),
          child: Center(child: NeonLogo(size: 34, radius: 10)),
        ),
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text('Nithi Bot', style: neonBrandTextStyle(fontSize: 16)),
            const SizedBox(height: 2),
            Text(
              '${appPages[_tab].label} · ${SessionService.displayName.isEmpty ? SessionService.username : SessionService.displayName}',
              style: const TextStyle(
                color: kNeonSubtle,
                fontSize: 10.5,
                fontWeight: FontWeight.w500,
                // Tighter than the title on purpose: at this size a wide halo
                // bleeds across the letterforms, same as `.brand .sub`.
                shadows: [
                  Shadow(color: Color(0x8C39FF14), blurRadius: 4),
                  Shadow(color: Color(0x4739FF14), blurRadius: 10),
                ],
              ),
            ),
          ],
        ),
        actions: [
          const RedBlueThemeToggle(compact: true),
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
                      style: TextStyle(color: muted, fontSize: 11),
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
      body: wide
          ? Row(
              children: [
                NavigationRail(
                  selectedIndex: _tab,
                  extended: MediaQuery.sizeOf(context).width >= 1120,
                  minExtendedWidth: 190,
                  backgroundColor: Theme.of(
                    context,
                  ).colorScheme.surface.withValues(alpha: .88),
                  indicatorColor: kNeon.withValues(alpha: .14),
                  onDestinationSelected: _selectTab,
                  leading: const Padding(
                    padding: EdgeInsets.only(top: 10, bottom: 16),
                    child: Icon(Icons.grid_view_rounded, color: kNeon),
                  ),
                  destinations: [
                    for (final page in appPages)
                      NavigationRailDestination(
                        icon: Icon(
                          page.icon,
                          color: kNeon.withValues(alpha: .72),
                        ),
                        selectedIcon: Icon(
                          page.icon,
                          color: kNeon,
                          shadows: kNeonIconGlowStrong,
                        ),
                        label: Text(page.navLabel),
                      ),
                  ],
                ),
                VerticalDivider(
                  width: 1,
                  color: Theme.of(context).dividerColor,
                ),
                Expanded(child: pageStack),
              ],
            )
          : pageStack,
      bottomNavigationBar: wide
          ? null
          : DecoratedBox(
              decoration: BoxDecoration(
                border: Border(
                  top: BorderSide(color: Theme.of(context).dividerColor),
                ),
              ),
              child: NavigationBar(
                selectedIndex: primaryIndex >= 0 ? primaryIndex : 4,
                onDestinationSelected: (index) {
                  if (index == 4) {
                    _showMore();
                  } else {
                    _selectTab(primaryPageIndexes[index]);
                  }
                },
                destinations: [
                  for (final index in primaryPageIndexes)
                    NavigationDestination(
                      icon: Icon(appPages[index].icon),
                      selectedIcon: Icon(appPages[index].icon),
                      label: appPages[index].navLabel,
                    ),
                  const NavigationDestination(
                    icon: Icon(Icons.grid_view_rounded),
                    selectedIcon: Icon(Icons.grid_view_rounded),
                    label: 'More',
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
            const NeonLogo(size: 72, radius: 16),
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
    final colors = Theme.of(context).colorScheme;
    return Scaffold(
      body: SafeArea(
        child: Stack(
          children: [
            Positioned(top: 8, right: 10, child: const RedBlueThemeToggle()),
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
                            const Align(child: NeonLogo(size: 76, radius: 16)),
                            const SizedBox(height: 18),
                            Text(
                              'Nithi Bot',
                              textAlign: TextAlign.center,
                              style: neonBrandTextStyle(fontSize: 24),
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
    required this.blue,
    required this.onSessionExpired,
    required this.onPageSelected,
  });

  final AppPageSpec page;
  final bool blue;
  final Future<void> Function() onSessionExpired;
  final ValueChanged<int> onPageSelected;

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
      ..setBackgroundColor(widget.blue ? kBlueBackground : kRedBackground)
      ..setNavigationDelegate(
        NavigationDelegate(
          onNavigationRequest: _handleNavigation,
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
    if (oldWidget.blue != widget.blue) {
      unawaited(
        _controller.setBackgroundColor(
          widget.blue ? kBlueBackground : kRedBackground,
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
            // The web dashboard keeps its existing compatibility values:
            // light selects Red and dark selects Blue.
            'theme': widget.blue ? 'dark' : 'light',
          },
        );
  }

  NavigationDecision _handleNavigation(NavigationRequest request) {
    final target = Uri.tryParse(request.url);
    final server = Uri.tryParse(SessionService.baseUrl);
    if (target == null || server == null) return NavigationDecision.navigate;
    final sameServer =
        target.scheme == server.scheme &&
        target.host == server.host &&
        target.port == server.port;
    if (!sameServer) return NavigationDecision.navigate;

    final targetIndex = appPages.indexWhere((page) => page.path == target.path);
    final currentIndex = appPages.indexOf(widget.page);
    if (targetIndex >= 0 && targetIndex != currentIndex) {
      scheduleMicrotask(() {
        if (mounted) widget.onPageSelected(targetIndex);
      });
      return NavigationDecision.prevent;
    }
    return NavigationDecision.navigate;
  }

  Future<void> _load() async {
    await WebAssetCache.prepare(_controller);
    await _controller.loadRequest(_pageUri);
  }

  Future<void> reload() async {
    setState(() {
      _error = null;
      _progress = 0;
    });
    await _controller.reload();
  }

  Future<void> _applyNativePresentation() async {
    final theme = widget.blue ? 'dark' : 'light';
    final background = widget.blue ? '#030914' : '#0d0608';
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
