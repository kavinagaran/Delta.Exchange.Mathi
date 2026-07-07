import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import '../services/api_service.dart';
import '../theme.dart';

class ConfigScreen extends StatefulWidget {
  const ConfigScreen({super.key});
  @override
  State<ConfigScreen> createState() => _ConfigScreenState();
}

class _ConfigScreenState extends State<ConfigScreen> {
  // Server
  final _cServer  = TextEditingController();
  // Strategy
  final _cLots    = TextEditingController();
  final _cStep    = TextEditingController();
  // Schedule
  final _cEH      = TextEditingController();
  final _cEM      = TextEditingController();
  final _cXH      = TextEditingController();
  final _cXM      = TextEditingController();
  // Telegram
  final _cTgToken = TextEditingController();
  final _cTgChat  = TextEditingController();

  bool _dryRun     = true;
  bool _tgEnabled  = true;
  bool _tgObscured = true;
  bool _loading    = false;
  bool _cfgLoaded  = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
  }

  @override
  void dispose() {
    for (final c in [_cServer, _cLots, _cStep, _cEH, _cEM, _cXH, _cXM, _cTgToken, _cTgChat]) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _load() async {
    final api = context.read<ApiService>();
    _cServer.text = api.baseUrl;
    setState(() => _loading = true);
    try {
      final cfg = await api.getConfig();
      _cLots.text    = cfg['STRADDLE_LOTS']?.toString() ?? '1000';
      _cStep.text    = cfg['STRIKE_STEP']?.toString() ?? '200';
      _cEH.text      = cfg['ENTRY_H_UTC']?.toString() ?? '12';
      _cEM.text      = cfg['ENTRY_M_UTC']?.toString() ?? '5';
      _cXH.text      = cfg['EXIT_H_UTC']?.toString() ?? '19';
      _cXM.text      = cfg['EXIT_M_UTC']?.toString() ?? '30';
      _cTgToken.text = cfg['TELEGRAM_BOT_TOKEN']?.toString() ?? '';
      _cTgChat.text  = cfg['TELEGRAM_CHAT_ID']?.toString() ?? '';
      _dryRun    = cfg['DRY_RUN']?.toString().toLowerCase() != 'false';
      _tgEnabled = cfg['TELEGRAM_ALERTS']?.toString().toLowerCase() != 'false';
      _cfgLoaded = true;
    } catch (_) {}
    if (mounted) setState(() => _loading = false);
  }

  Future<void> _save() async {
    final api = context.read<ApiService>();
    final lots = int.tryParse(_cLots.text.trim()) ?? 1000;
    if (lots > 1000) {
      _cLots.text = '1000';
      _showSnack('Lots capped at 1000', AppColors.red);
      return;
    }
    setState(() => _loading = true);
    try {
      await api.setBaseUrl(_cServer.text.trim());
      if (_cfgLoaded) {
        await api.saveConfig({
          'STRADDLE_LOTS':      _cLots.text.trim(),
          'STRIKE_STEP':        _cStep.text.trim(),
          'ENTRY_H_UTC':        _cEH.text.trim(),
          'ENTRY_M_UTC':        _cEM.text.trim(),
          'EXIT_H_UTC':         _cXH.text.trim(),
          'EXIT_M_UTC':         _cXM.text.trim(),
          'DRY_RUN':            _dryRun ? 'true' : 'false',
          'TELEGRAM_BOT_TOKEN': _cTgToken.text.trim(),
          'TELEGRAM_CHAT_ID':   _cTgChat.text.trim(),
          'TELEGRAM_ALERTS':    _tgEnabled ? 'true' : 'false',
        });
      }
      _showSnack('Configuration saved', AppColors.green);
    } catch (e) {
      _showSnack('Error: $e', AppColors.red);
    }
    if (mounted) setState(() => _loading = false);
  }

  Future<void> _testTelegram() async {
    final token  = _cTgToken.text.trim();
    final chatid = _cTgChat.text.trim();
    if (token.isEmpty || chatid.isEmpty) {
      _showSnack('Enter Bot Token and Chat ID first', AppColors.red);
      return;
    }
    setState(() => _loading = true);
    try {
      final api = context.read<ApiService>();
      // Save Telegram config first so the server uses the latest values
      await api.saveConfig({
        'TELEGRAM_BOT_TOKEN': token,
        'TELEGRAM_CHAT_ID':   chatid,
        'TELEGRAM_ALERTS':    _tgEnabled ? 'true' : 'false',
      });
      final res = await api.testTelegram();
      if (res['ok'] == true) {
        _showSnack('✈ Test message sent! Check your Telegram.', AppColors.green);
      } else {
        _showSnack('Telegram error: ${res['error'] ?? 'unknown'}', AppColors.red);
      }
    } catch (e) {
      _showSnack('Error: $e', AppColors.red);
    }
    if (mounted) setState(() => _loading = false);
  }

  void _showSnack(String msg, Color color) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(msg),
      backgroundColor: color.withOpacity(0.9),
    ));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Configuration'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh_rounded, color: AppColors.cyan),
            onPressed: _loading ? null : _load,
            tooltip: 'Reload from server',
          ),
        ],
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator(color: AppColors.cyan))
          : SingleChildScrollView(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 30),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [

                // ── SERVER ──────────────────────────────────────
                _section('SERVER CONNECTION', [
                  _field(_cServer, 'Flask dashboard URL',
                      hint: 'http://192.168.x.x:5000', keyType: TextInputType.url),
                  _hint('Phone and PC must be on the same Wi-Fi network.'),
                ]),

                // ── STRATEGY ────────────────────────────────────
                _section('STRATEGY', [
                  Row(children: [
                    Expanded(child: _field(_cLots, 'Lots', hint: '1000', keyType: TextInputType.number)),
                    const SizedBox(width: 12),
                    Expanded(child: _field(_cStep, 'Strike Step (\$)', hint: '200', keyType: TextInputType.number)),
                  ]),
                ]),

                // ── SCHEDULE ─────────────────────────────────────
                _section('SCHEDULE (UTC)', [
                  Row(children: [
                    Expanded(child: _field(_cEH, 'Entry Hour', hint: '12', keyType: TextInputType.number)),
                    const SizedBox(width: 12),
                    Expanded(child: _field(_cEM, 'Entry Min', hint: '5', keyType: TextInputType.number)),
                  ]),
                  const SizedBox(height: 4),
                  _hint('Entry = 12:05 UTC = 5:35 PM IST'),
                  const SizedBox(height: 10),
                  Row(children: [
                    Expanded(child: _field(_cXH, 'Exit Hour', hint: '19', keyType: TextInputType.number)),
                    const SizedBox(width: 12),
                    Expanded(child: _field(_cXM, 'Exit Min', hint: '30', keyType: TextInputType.number)),
                  ]),
                  const SizedBox(height: 4),
                  _hint('Exit = 19:30 UTC = 1:00 AM IST'),
                ]),

                // ── TRADING MODE ─────────────────────────────────
                _section('TRADING MODE', [
                  AnimatedContainer(
                    duration: const Duration(milliseconds: 200),
                    decoration: BoxDecoration(
                      color: (_dryRun ? AppColors.red : AppColors.green).withOpacity(0.07),
                      borderRadius: BorderRadius.circular(10),
                      border: Border.all(color: (_dryRun ? AppColors.red : AppColors.green).withOpacity(0.3)),
                    ),
                    child: SwitchListTile(
                      title: Text(
                        _dryRun ? '⚠  DRY-RUN — simulation only' : '●  LIVE — real orders',
                        style: TextStyle(color: _dryRun ? AppColors.red : AppColors.green,
                            fontSize: 13, fontWeight: FontWeight.w700),
                      ),
                      subtitle: Text(
                        _dryRun
                            ? 'No real orders placed on Delta Exchange'
                            : 'Real orders will execute on your account',
                        style: TextStyle(color: (_dryRun ? AppColors.red : AppColors.green).withOpacity(0.6), fontSize: 11),
                      ),
                      value: !_dryRun,
                      activeColor: AppColors.green,
                      inactiveThumbColor: AppColors.red,
                      inactiveTrackColor: AppColors.red.withOpacity(0.25),
                      onChanged: (v) => setState(() => _dryRun = !v),
                    ),
                  ),
                ]),

                // ── TELEGRAM ─────────────────────────────────────
                _section('TELEGRAM ALERTS', [
                  // Enable toggle
                  AnimatedContainer(
                    duration: const Duration(milliseconds: 200),
                    margin: const EdgeInsets.only(bottom: 12),
                    decoration: BoxDecoration(
                      color: (_tgEnabled ? AppColors.cyan : AppColors.sub).withOpacity(0.07),
                      borderRadius: BorderRadius.circular(10),
                      border: Border.all(color: (_tgEnabled ? AppColors.cyan : AppColors.sub).withOpacity(0.3)),
                    ),
                    child: SwitchListTile(
                      title: Text(
                        _tgEnabled ? '✈  ENABLED — alerts will fire' : '✈  DISABLED',
                        style: TextStyle(color: _tgEnabled ? AppColors.cyan : AppColors.sub,
                            fontSize: 13, fontWeight: FontWeight.w700),
                      ),
                      subtitle: const Text(
                        'Entry, exit, and error notifications',
                        style: TextStyle(color: AppColors.sub, fontSize: 11),
                      ),
                      value: _tgEnabled,
                      activeColor: AppColors.cyan,
                      onChanged: (v) => setState(() => _tgEnabled = v),
                    ),
                  ),

                  // Bot Token
                  TextField(
                    controller: _cTgToken,
                    obscureText: _tgObscured,
                    style: const TextStyle(color: AppColors.text, fontSize: 13,
                        fontFamily: 'monospace'),
                    decoration: InputDecoration(
                      labelText: 'Bot Token',
                      hintText: '123456789:ABC-DEFghi...',
                      suffixIcon: IconButton(
                        icon: Icon(_tgObscured ? Icons.visibility_outlined : Icons.visibility_off_outlined,
                            color: AppColors.sub, size: 18),
                        onPressed: () => setState(() => _tgObscured = !_tgObscured),
                      ),
                    ),
                  ),
                  const SizedBox(height: 10),

                  // Chat ID with paste button
                  TextField(
                    controller: _cTgChat,
                    keyboardType: TextInputType.number,
                    style: const TextStyle(color: AppColors.text, fontSize: 13,
                        fontFamily: 'monospace'),
                    decoration: InputDecoration(
                      labelText: 'Chat ID',
                      hintText: '-100xxxxxxxxxx',
                      suffixIcon: IconButton(
                        icon: const Icon(Icons.content_paste_rounded, color: AppColors.sub, size: 18),
                        tooltip: 'Paste',
                        onPressed: () async {
                          final data = await Clipboard.getData('text/plain');
                          if (data?.text != null) _cTgChat.text = data!.text!.trim();
                        },
                      ),
                    ),
                  ),
                  const SizedBox(height: 10),

                  // How to get Chat ID hint
                  Container(
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: AppColors.lift,
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(color: AppColors.border),
                    ),
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      const Text('How to set up:', style: TextStyle(color: AppColors.text, fontSize: 12, fontWeight: FontWeight.w600)),
                      const SizedBox(height: 6),
                      _step('1', 'Open Telegram → search @BotFather → /newbot'),
                      _step('2', 'Copy the token it gives you → paste above'),
                      _step('3', 'Send any message to your new bot'),
                      _step('4', 'Visit: api.telegram.org/bot<TOKEN>/getUpdates'),
                      _step('5', 'Find "chat":{"id":...} → paste that number above'),
                    ]),
                  ),

                  const SizedBox(height: 12),

                  // Test button
                  SizedBox(
                    width: double.infinity,
                    child: OutlinedButton.icon(
                      onPressed: _loading ? null : _testTelegram,
                      icon: const Icon(Icons.send_rounded, size: 16),
                      label: const Text('SEND TEST MESSAGE',
                          style: TextStyle(fontWeight: FontWeight.w800, letterSpacing: 1)),
                      style: OutlinedButton.styleFrom(
                        foregroundColor: AppColors.cyan,
                        side: const BorderSide(color: AppColors.cyan),
                        padding: const EdgeInsets.symmetric(vertical: 13),
                        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                      ),
                    ),
                  ),
                ]),

                // ── SAVE ALL ──────────────────────────────────────
                SizedBox(
                  width: double.infinity,
                  height: 50,
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      gradient: const LinearGradient(
                          colors: [AppColors.blue, AppColors.purple],
                          begin: Alignment.centerLeft, end: Alignment.centerRight),
                      borderRadius: BorderRadius.circular(10),
                      boxShadow: [BoxShadow(color: AppColors.blue.withOpacity(0.35), blurRadius: 16, offset: const Offset(0, 5))],
                    ),
                    child: ElevatedButton(
                      style: ElevatedButton.styleFrom(
                        backgroundColor: Colors.transparent, shadowColor: Colors.transparent,
                        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
                      ),
                      onPressed: _loading ? null : _save,
                      child: const Text('SAVE ALL CONFIGURATION',
                          style: TextStyle(color: Colors.white, fontWeight: FontWeight.w900,
                              fontSize: 14, letterSpacing: 1.5)),
                    ),
                  ),
                ),
              ]),
            ),
    );
  }

  Widget _section(String title, List<Widget> children) => Padding(
    padding: const EdgeInsets.only(bottom: 20),
    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Padding(
        padding: const EdgeInsets.only(bottom: 12),
        child: Row(children: [
          Text(title, style: const TextStyle(color: AppColors.sub, fontSize: 10, letterSpacing: 2)),
          const SizedBox(width: 10),
          const Expanded(child: Divider(color: AppColors.border)),
        ]),
      ),
      ...children,
    ]),
  );

  Widget _field(TextEditingController ctrl, String label, {String? hint, TextInputType? keyType}) =>
    TextField(
      controller: ctrl, keyboardType: keyType,
      style: const TextStyle(color: AppColors.text, fontSize: 14),
      decoration: InputDecoration(labelText: label, hintText: hint),
    );

  Widget _hint(String text) => Padding(
    padding: const EdgeInsets.only(top: 4, left: 2),
    child: Text(text, style: const TextStyle(color: AppColors.sub, fontSize: 11)),
  );

  Widget _step(String n, String text) => Padding(
    padding: const EdgeInsets.only(top: 4),
    child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Container(
        width: 16, height: 16, margin: const EdgeInsets.only(right: 8, top: 1),
        alignment: Alignment.center,
        decoration: BoxDecoration(color: AppColors.cyan.withOpacity(0.15),
            borderRadius: BorderRadius.circular(4)),
        child: Text(n, style: const TextStyle(color: AppColors.cyan, fontSize: 9, fontWeight: FontWeight.w800)),
      ),
      Expanded(child: Text(text, style: const TextStyle(color: AppColors.sub, fontSize: 11))),
    ]),
  );
}
