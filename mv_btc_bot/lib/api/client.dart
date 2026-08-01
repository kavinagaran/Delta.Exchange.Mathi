/// Typed access to the dashboard's JSON API.
///
/// Authentication reuses the Flask session cookie the existing login flow
/// already obtains — the app never holds the account password beyond that
/// exchange, and never holds a Delta API key at all.
///
/// Every call returns a result rather than throwing into a widget: a trading
/// screen that renders a stack trace is worse than one that says the server is
/// unreachable, and "unreachable" must never be mistaken for "flat".
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart' show immutable, visibleForTesting;
import 'package:http/http.dart' as http;

/// Outcome of one request. [data] is only meaningful when [ok].
@immutable
class ApiResult<T> {
  const ApiResult.ok(this.data)
      : error = null,
        unauthorised = false;
  const ApiResult.failed(this.error, {this.unauthorised = false}) : data = null;

  final T? data;
  final String? error;
  final bool unauthorised;

  bool get ok => error == null;
}

class DashboardApi {
  DashboardApi({required this.baseUrl, required this.sessionCookie});

  final String baseUrl;
  final String? sessionCookie;

  static const _timeout = Duration(seconds: 15);

  /// Request headers, public so the authentication contract is testable.
  ///
  /// The Cookie header is omitted entirely when there is no session rather
  /// than sent empty: `Cookie: session=` looks authenticated to Flask and
  /// fails in a more confusing way than being plainly anonymous.
  @visibleForTesting
  Map<String, String> get headers => {
        'Accept': 'application/json',
        if (sessionCookie != null && sessionCookie!.isNotEmpty)
          'Cookie': 'session=$sessionCookie',
      };

  Future<ApiResult<dynamic>> get(String path) async {
    final uri = Uri.parse('$baseUrl$path');
    try {
      final response = await http.get(uri, headers: headers).timeout(_timeout);
      if (response.statusCode == 401 || response.statusCode == 403) {
        return const ApiResult.failed('Session expired', unauthorised: true);
      }
      if (response.statusCode >= 400) {
        return ApiResult.failed('Server returned ${response.statusCode}');
      }
      if (response.body.isEmpty) return const ApiResult.ok(null);
      return ApiResult.ok(jsonDecode(response.body));
    } on TimeoutException {
      return const ApiResult.failed('Timed out — the server did not respond');
    } catch (error) {
      // Deliberately broad: a widget must not see a socket exception. The
      // message is surfaced verbatim so a DNS or TLS problem stays diagnosable.
      return ApiResult.failed('Cannot reach the server: $error');
    }
  }

  Future<ApiResult<Map<String, dynamic>>> getMap(String path) async {
    final result = await get(path);
    if (!result.ok) {
      return ApiResult.failed(result.error, unauthorised: result.unauthorised);
    }
    final data = result.data;
    if (data is Map<String, dynamic>) return ApiResult.ok(data);
    return const ApiResult.failed('Unexpected response shape');
  }

  Future<ApiResult<List<dynamic>>> getList(String path) async {
    final result = await get(path);
    if (!result.ok) {
      return ApiResult.failed(result.error, unauthorised: result.unauthorised);
    }
    final data = result.data;
    if (data is List) return ApiResult.ok(data);
    // Several endpoints wrap their rows; unwrap the common shapes rather than
    // making each screen guess.
    if (data is Map<String, dynamic>) {
      for (final key in ['trades', 'rows', 'items', 'data', 'candles']) {
        final value = data[key];
        if (value is List) return ApiResult.ok(value);
      }
    }
    return const ApiResult.failed('Unexpected response shape');
  }

  // ── endpoints the native screens use ──────────────────────────────────

  Future<ApiResult<Map<String, dynamic>>> status() => getMap('/api/status');
  Future<ApiResult<Map<String, dynamic>>> summary() => getMap('/api/summary');
  Future<ApiResult<Map<String, dynamic>>> wallet() => getMap('/api/wallet');
  Future<ApiResult<List<dynamic>>> todayTrades() => getList('/api/today-trades');
  Future<ApiResult<List<dynamic>>> allPositions() => getList('/api/all-positions');
  Future<ApiResult<Map<String, dynamic>>> engineSnapshot() =>
      getMap('/api/engine/snapshot');
  Future<ApiResult<Map<String, dynamic>>> engineHealth() =>
      getMap('/api/engine/health');
  Future<ApiResult<Map<String, dynamic>>> scoreAutoStatus() =>
      getMap('/api/trend-engine/score-auto/status');
  Future<ApiResult<Map<String, dynamic>>> tradingMode() =>
      getMap('/api/trading-mode-availability');
}
