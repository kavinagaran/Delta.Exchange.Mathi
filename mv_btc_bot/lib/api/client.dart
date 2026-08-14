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
  const ApiResult.ok(this.data) : error = null, unauthorised = false;
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
    'Content-Type': 'application/json',
    if (sessionCookie != null && sessionCookie!.isNotEmpty)
      'Cookie': 'session=$sessionCookie',
  };

  Future<ApiResult<dynamic>> _request(
    String method,
    String path, {
    Map<String, dynamic>? body,
  }) async {
    final uri = Uri.parse('$baseUrl$path');
    try {
      final response = switch (method) {
        'POST' =>
          await http
              .post(uri, headers: headers, body: jsonEncode(body ?? const {}))
              .timeout(_timeout),
        'DELETE' =>
          await http
              .delete(uri, headers: headers, body: jsonEncode(body ?? const {}))
              .timeout(_timeout),
        _ => await http.get(uri, headers: headers).timeout(_timeout),
      };
      if (response.statusCode == 401 || response.statusCode == 403) {
        return const ApiResult.failed('Session expired', unauthorised: true);
      }
      dynamic decoded;
      if (response.body.isNotEmpty) {
        try {
          decoded = jsonDecode(response.body);
        } catch (_) {
          decoded = null;
        }
      }
      if (response.statusCode >= 400) {
        final message = decoded is Map ? decoded['error']?.toString() : null;
        return ApiResult.failed(
          message?.isNotEmpty == true
              ? message
              : 'Server returned ${response.statusCode}',
        );
      }
      return ApiResult.ok(decoded);
    } on TimeoutException {
      return const ApiResult.failed('Timed out — the server did not respond');
    } catch (error) {
      return ApiResult.failed('Cannot reach the server: $error');
    }
  }

  Future<ApiResult<dynamic>> get(String path) async {
    return _request('GET', path);
  }

  Future<ApiResult<dynamic>> post(String path, [Map<String, dynamic>? body]) =>
      _request('POST', path, body: body);

  Future<ApiResult<dynamic>> delete(
    String path, [
    Map<String, dynamic>? body,
  ]) => _request('DELETE', path, body: body);

  Future<ApiResult<Map<String, dynamic>>> getMap(String path) async {
    final result = await get(path);
    if (!result.ok) {
      return ApiResult.failed(result.error, unauthorised: result.unauthorised);
    }
    final data = result.data;
    if (data is Map<String, dynamic>) return ApiResult.ok(data);
    return const ApiResult.failed('Unexpected response shape');
  }

  Future<ApiResult<Map<String, dynamic>>> postMap(
    String path, [
    Map<String, dynamic>? body,
  ]) async {
    final result = await post(path, body);
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
      for (final key in [
        'trades',
        'records',
        'rows',
        'items',
        'data',
        'candles',
      ]) {
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
  Future<ApiResult<List<dynamic>>> todayTrades() =>
      getList('/api/today-trades');
  Future<ApiResult<List<dynamic>>> trades() => getList('/api/trades');
  Future<ApiResult<List<dynamic>>> performanceTrades() =>
      getList('/api/performance/delta-trades');
  Future<ApiResult<List<dynamic>>> allPositions() =>
      getList('/api/all-positions');
  Future<ApiResult<Map<String, dynamic>>> engineSnapshot() =>
      getMap('/api/engine/snapshot');
  Future<ApiResult<Map<String, dynamic>>> engineHealth() =>
      getMap('/api/engine/health');
  Future<ApiResult<Map<String, dynamic>>> scoreAutoStatus() =>
      getMap('/api/trend-engine/score-auto/status');
  Future<ApiResult<Map<String, dynamic>>> protectionStatus() =>
      getMap('/api/tp-monitor');

  /// Live, account-scoped protection telemetry from the dashboard.
  ///
  /// Delta credentials remain server-side. The native client receives only
  /// the same safe mark/P&L/protection fields exposed on Today in the web UI.
  Stream<ApiResult<Map<String, dynamic>>> protectionStream() async* {
    final client = http.Client();
    try {
      final request = http.Request(
        'GET',
        Uri.parse('$baseUrl/api/stream/protection'),
      );
      request.headers.addAll({
        ...headers,
        'Accept': 'text/event-stream',
        'Cache-Control': 'no-cache',
      });
      final response = await client.send(request).timeout(_timeout);
      if (response.statusCode == 401 || response.statusCode == 403) {
        yield const ApiResult.failed('Session expired', unauthorised: true);
        return;
      }
      if (response.statusCode >= 400) {
        yield ApiResult.failed('Server returned ${response.statusCode}');
        return;
      }
      await for (final line
          in response.stream
              .transform(utf8.decoder)
              .transform(const LineSplitter())) {
        if (!line.startsWith('data:')) continue;
        try {
          final decoded = jsonDecode(line.substring(5).trim());
          if (decoded is Map<String, dynamic>) {
            yield ApiResult.ok(decoded);
          }
        } catch (_) {
          // Ignore one damaged SSE frame; the next complete snapshot repairs
          // the display without terminating the long-lived connection.
        }
      }
    } on TimeoutException {
      yield const ApiResult.failed('Live protection stream timed out');
    } catch (error) {
      yield ApiResult.failed('Live protection stream unavailable: $error');
    } finally {
      client.close();
    }
  }

  Future<ApiResult<Map<String, dynamic>>> tradingMode() =>
      getMap('/api/trading-mode-availability');
  Future<ApiResult<Map<String, dynamic>>> engineLive() =>
      getMap('/api/engine/live');
  Future<ApiResult<Map<String, dynamic>>> engineStatus() =>
      getMap('/api/engine/status');
  Future<ApiResult<Map<String, dynamic>>> decisionHistory() =>
      getMap('/api/engine/decision-history');

  Future<ApiResult<Map<String, dynamic>>> dryStatus() =>
      getMap('/api/dry-run/status');
  Future<ApiResult<List<dynamic>>> dryTrades() =>
      getList('/api/dry-run/trades');
  Future<ApiResult<List<dynamic>>> dryTodayTrades() =>
      getList('/api/dry-run/today-trades');
  Future<ApiResult<Map<String, dynamic>>> drySummary() =>
      getMap('/api/dry-run/summary');

  Future<ApiResult<Map<String, dynamic>>> config() => getMap('/api/config');
  Future<ApiResult<dynamic>> saveConfig(Map<String, dynamic> values) =>
      post('/api/config', values);
  Future<ApiResult<dynamic>> resetZoneLock() =>
      post('/api/trend-engine/score-auto/setup-lock/reset');
  Future<ApiResult<dynamic>> testTelegram() => post('/api/test-telegram');

  Future<ApiResult<Map<String, dynamic>>> logs({int limit = 100}) =>
      getMap('/api/logs?n=${limit.clamp(1, 500)}');
  Future<ApiResult<List<dynamic>>> accounts() => getList('/api/accounts');
  Future<ApiResult<Map<String, dynamic>>> bots() => getMap('/api/bots');
  Future<ApiResult<dynamic>> saveAccount(Map<String, dynamic> values) =>
      post('/api/accounts', values);
  Future<ApiResult<dynamic>> testAccount(Map<String, dynamic> values) =>
      post('/api/accounts/test', values);
  Future<ApiResult<dynamic>> deleteAccount(String username) =>
      delete('/api/accounts/${Uri.encodeComponent(username)}');
  Future<ApiResult<dynamic>> setBotActive(String username, bool active) => post(
    '/api/bots/${Uri.encodeComponent(username)}/${active ? 'start' : 'stop'}',
  );

  /// Server-authoritative Cockpit setup eligibility and allowed strategies.
  Future<ApiResult<Map<String, dynamic>>> cockpitSetups() =>
      getMap('/api/cockpit/setups');

  /// Resolve the exact contract/price a Cockpit trade would use right now,
  /// without submitting anything (read-only preview). The setup is mandatory:
  /// the server revalidates it rather than trusting a green client-side tile.
  Future<ApiResult<Map<String, dynamic>>> cockpitPreview(
    String action,
    String setup,
  ) => postMap('/api/cockpit/preview', {'action': action, 'setup': setup});

  /// Place one setup-gated manual LIVE Cockpit trade: buy or sell CE/PE/MOVE.
  /// Reuses the same execution seam and exclusivity as the automated
  /// controller, tagged with manual ownership so the controller never manages
  /// or replaces it.
  Future<ApiResult<Map<String, dynamic>>> cockpitEnter(
    String action,
    String setup,
  ) => postMap('/api/cockpit/enter', {'action': action, 'setup': setup});

  Future<ApiResult<dynamic>> squareOff({
    required String slot,
    required String targetMode,
  }) => post('/api/square-off?slot=${Uri.encodeQueryComponent(slot)}', {
    'target_mode': targetMode,
  });
}
