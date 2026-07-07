class Trade {
  final String date;
  final String symbol;
  final double btcEntry;
  final double btcExit;
  final double btcMovePct;
  final double entryMark;
  final double exitMark;
  final double pnlUsd;
  final double costUsd;
  final String entryTime;
  final String exitTime;
  final String? source;
  double cumPnl = 0;

  Trade({
    required this.date,
    required this.symbol,
    required this.btcEntry,
    required this.btcExit,
    required this.btcMovePct,
    required this.entryMark,
    required this.exitMark,
    required this.pnlUsd,
    required this.costUsd,
    required this.entryTime,
    required this.exitTime,
    this.source,
  });

  factory Trade.fromJson(Map<String, dynamic> j) => Trade(
    date:       j['date']?.toString() ?? '',
    symbol:     j['symbol']?.toString() ?? '',
    btcEntry:   _f(j['btc_entry']),
    btcExit:    _f(j['btc_exit']),
    btcMovePct: _f(j['btc_move_pct']),
    entryMark:  _f(j['entry_mark']),
    exitMark:   _f(j['exit_mark']),
    pnlUsd:     _f(j['pnl_usd']),
    costUsd:    _f(j['cost_usd']),
    entryTime:  j['entry_time']?.toString() ?? '',
    exitTime:   j['exit_time']?.toString() ?? '',
    source:     j['source']?.toString(),
  );

  bool get isWin => pnlUsd >= 0;
  bool get isSim => source == 'backtest';

  static double _f(dynamic v) =>
      v == null ? 0.0 : double.tryParse(v.toString()) ?? 0.0;
}
