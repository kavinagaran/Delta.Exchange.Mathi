import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import '../models/position.dart';
import '../models/trade.dart';

class ApiService extends ChangeNotifier {
  String _baseUrl = 'http://192.168.1.100:5000';
  Position? _position;
  Summary?  _summary;
  List<Trade> _trades = [];
  String? _error;
  bool _loading = false;
  DateTime? _lastUpdated;

  String      get baseUrl     => _baseUrl;
  Position?   get position    => _position;
  Summary?    get summary     => _summary;
  List<Trade> get trades      => _trades;
  String?     get error       => _error;
  bool        get loading     => _loading;
  DateTime?   get lastUpdated => _lastUpdated;

  ApiService() { _loadBaseUrl(); }

  Future<void> _loadBaseUrl() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getString('base_url');
    if (saved != null) _baseUrl = saved;
    notifyListeners();
  }

  Future<void> setBaseUrl(String url) async {
    _baseUrl = url.trim().replaceAll(RegExp(r'/$'), '');
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('base_url', _baseUrl);
    notifyListeners();
  }

  Future<void> refresh() async {
    if (_loading) return;
    _loading = true;
    _error = null;
    notifyListeners();
    try {
      await Future.wait([_fetchStatus(), _fetchSummary(), _fetchTrades()]);
      _lastUpdated = DateTime.now().toUtc();
      _error = null;
    } catch (e) {
      _error = e.toString().replaceFirst('Exception: ', '');
    } finally {
      _loading = false;
      notifyListeners();
    }
  }

  Future<void> _fetchStatus() async {
    final r = await http
        .get(Uri.parse('$_baseUrl/api/status'))
        .timeout(const Duration(seconds: 8));
    _position = Position.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<void> _fetchSummary() async {
    final r = await http
        .get(Uri.parse('$_baseUrl/api/summary'))
        .timeout(const Duration(seconds: 8));
    final data = json.decode(r.body);
    if (data is Map && (data as Map).isNotEmpty) {
      _summary = Summary.fromJson(data as Map<String, dynamic>);
    }
  }

  Future<void> _fetchTrades() async {
    final r = await http
        .get(Uri.parse('$_baseUrl/api/trades'))
        .timeout(const Duration(seconds: 8));
    final list = (json.decode(r.body) as List).cast<Map<String, dynamic>>();
    double cum = 0;
    _trades = list.map((j) {
      final t = Trade.fromJson(j);
      cum += t.pnlUsd;
      t.cumPnl = cum;
      return t;
    }).toList();
  }

  Future<Map<String, dynamic>> getConfig() async {
    final r = await http
        .get(Uri.parse('$_baseUrl/api/config'))
        .timeout(const Duration(seconds: 8));
    return json.decode(r.body) as Map<String, dynamic>;
  }

  Future<void> saveConfig(Map<String, String> cfg) async {
    await http
        .post(
          Uri.parse('$_baseUrl/api/config'),
          headers: {'Content-Type': 'application/json'},
          body: json.encode(cfg),
        )
        .timeout(const Duration(seconds: 8));
  }

  Future<Map<String, dynamic>> testTelegram() async {
    final r = await http
        .post(Uri.parse('$_baseUrl/api/test-telegram'))
        .timeout(const Duration(seconds: 12));
    return json.decode(r.body) as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> importBacktest() async {
    final r = await http
        .post(Uri.parse('$_baseUrl/api/import-backtest'))
        .timeout(const Duration(seconds: 20));
    return json.decode(r.body) as Map<String, dynamic>;
  }
}
