class Position {
  final String status;
  final String? entryDate;
  final String? symbol;
  final double? strike;
  final int? lots;
  final double? entryMark;
  final double? currentMark;
  final double? livePnl;
  final double? btcAtEntry;
  final double? totalCostUsd;
  final String? entryTimeUtc;
  final String? settlement;

  Position({
    required this.status,
    this.entryDate,
    this.symbol,
    this.strike,
    this.lots,
    this.entryMark,
    this.currentMark,
    this.livePnl,
    this.btcAtEntry,
    this.totalCostUsd,
    this.entryTimeUtc,
    this.settlement,
  });

  factory Position.fromJson(Map<String, dynamic> j) => Position(
    status:       j['status']?.toString() ?? 'IDLE',
    entryDate:    j['entry_date']?.toString(),
    symbol:       j['symbol']?.toString(),
    strike:       _f(j['strike']),
    lots:         j['lots'] is int ? j['lots'] as int : int.tryParse(j['lots']?.toString() ?? ''),
    entryMark:    _f(j['entry_mark']),
    currentMark:  _f(j['current_mark']),
    livePnl:      _f(j['live_pnl']),
    btcAtEntry:   _f(j['btc_at_entry']),
    totalCostUsd: _f(j['total_cost_usd']),
    entryTimeUtc: j['entry_time_utc']?.toString(),
    settlement:   j['settlement']?.toString(),
  );

  bool get isOpen => status == 'OPEN';

  static double? _f(dynamic v) =>
      v == null ? null : double.tryParse(v.toString());
}

class Summary {
  final int totalDays;
  final int wins;
  final int losses;
  final double winRate;
  final double totalPnl;
  final double avgWin;
  final double avgLoss;
  final double rr;
  final double maxDd;

  Summary({
    required this.totalDays,
    required this.wins,
    required this.losses,
    required this.winRate,
    required this.totalPnl,
    required this.avgWin,
    required this.avgLoss,
    required this.rr,
    required this.maxDd,
  });

  factory Summary.fromJson(Map<String, dynamic> j) => Summary(
    totalDays: j['total_days'] is int ? j['total_days'] : int.tryParse(j['total_days']?.toString() ?? '') ?? 0,
    wins:      j['wins'] is int ? j['wins'] : int.tryParse(j['wins']?.toString() ?? '') ?? 0,
    losses:    j['losses'] is int ? j['losses'] : int.tryParse(j['losses']?.toString() ?? '') ?? 0,
    winRate:   _f(j['win_rate']),
    totalPnl:  _f(j['total_pnl']),
    avgWin:    _f(j['avg_win']),
    avgLoss:   _f(j['avg_loss']),
    rr:        _f(j['rr']),
    maxDd:     _f(j['max_dd']),
  );

  static double _f(dynamic v) =>
      double.tryParse(v?.toString() ?? '') ?? 0.0;
}
