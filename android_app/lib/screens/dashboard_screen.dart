import 'dart:async';
import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';
import '../models/position.dart';
import '../models/trade.dart';
import '../services/api_service.dart';
import '../theme.dart';

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key});
  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      context.read<ApiService>().refresh();
    });
    _timer = Timer.periodic(const Duration(seconds: 30), (_) {
      if (mounted) context.read<ApiService>().refresh();
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<ApiService>(builder: (ctx, api, _) {
      return Scaffold(
        appBar: AppBar(
          title: const Text('△ MV-BTC BOT',
              style: TextStyle(color: AppColors.blue, fontWeight: FontWeight.w800, letterSpacing: 2)),
          actions: [
            if (api.lastUpdated != null)
              Center(
                child: Padding(
                  padding: const EdgeInsets.only(right: 4),
                  child: Text(
                    DateFormat('HH:mm').format(api.lastUpdated!.toLocal()),
                    style: const TextStyle(color: AppColors.sub, fontSize: 11),
                  ),
                ),
              ),
            IconButton(
              icon: api.loading
                  ? const SizedBox(
                      width: 18, height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2, color: AppColors.cyan))
                  : const Icon(Icons.refresh_rounded, color: AppColors.cyan),
              onPressed: api.loading ? null : api.refresh,
            ),
          ],
        ),
        body: api.error != null && api.position == null
            ? _buildError(api)
            : RefreshIndicator(
                color: AppColors.cyan,
                backgroundColor: AppColors.card,
                onRefresh: api.refresh,
                child: CustomScrollView(
                  slivers: [
                    SliverToBoxAdapter(child: _buildPositionCard(api.position)),
                    SliverToBoxAdapter(child: _buildStatsGrid(api.summary)),
                    if (api.trades.isNotEmpty)
                      SliverToBoxAdapter(child: _buildChart(api.trades)),
                    const SliverToBoxAdapter(child: SizedBox(height: 20)),
                  ],
                ),
              ),
      );
    });
  }

  Widget _buildError(ApiService api) => Center(
        child: Padding(
          padding: const EdgeInsets.all(28),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const Icon(Icons.wifi_off_rounded, color: AppColors.red, size: 52),
            const SizedBox(height: 14),
            const Text('Cannot reach server',
                style: TextStyle(color: AppColors.text, fontSize: 17, fontWeight: FontWeight.bold)),
            const SizedBox(height: 8),
            Text(api.error ?? '',
                style: const TextStyle(color: AppColors.sub, fontSize: 12),
                textAlign: TextAlign.center),
            const SizedBox(height: 20),
            ElevatedButton.icon(
              onPressed: api.refresh,
              icon: const Icon(Icons.refresh),
              label: const Text('Retry'),
              style: ElevatedButton.styleFrom(backgroundColor: AppColors.blue, foregroundColor: Colors.white),
            ),
          ]),
        ),
      );

  // ── Position Card ────────────────────────────────────────────
  Widget _buildPositionCard(Position? pos) {
    final isOpen = pos?.isOpen ?? false;
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 14, 14, 6),
      child: Card(
        child: Container(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(12),
            border: Border(top: BorderSide(
              color: isOpen ? AppColors.cyan : AppColors.border, width: 3,
            )),
          ),
          padding: const EdgeInsets.all(16),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              const Text('POSITION', style: TextStyle(color: AppColors.sub, fontSize: 11, letterSpacing: 2)),
              const Spacer(),
              _statusPill(isOpen),
            ]),
            const SizedBox(height: 14),
            if (isOpen && pos != null) ...[
              _posRow('Symbol',     pos.symbol ?? '—',                         color: AppColors.cyan),
              _posRow('Strike',     pos.strike != null ? '\$${_comma(pos.strike!)}' : '—'),
              _posRow('Lots',       pos.lots?.toString() ?? '—'),
              _posRow('Entry Mark', pos.entryMark != null ? '\$${pos.entryMark!.toStringAsFixed(4)}' : '—'),
              _posRow('Mark Now',   pos.currentMark != null ? '\$${pos.currentMark!.toStringAsFixed(4)}' : '—', color: AppColors.gold),
              _posRow('BTC Entry',  pos.btcAtEntry != null ? '\$${_comma(pos.btcAtEntry!)}' : '—'),
              _posRow('Cost',       pos.totalCostUsd != null ? '\$${pos.totalCostUsd!.toStringAsFixed(2)}' : '—'),
              _posRow('Entry UTC',  pos.entryTimeUtc ?? '—', color: AppColors.sub),
              _posRow('Settles',    pos.settlement?.replaceFirst('T', '  ').replaceFirst('Z', ' UTC') ?? '—', color: AppColors.sub),
              const SizedBox(height: 10),
              _pnlRow(pos.livePnl),
            ] else ...[
              _schedRow('Entry', '5:35 PM IST', '12:05 UTC', AppColors.green),
              const SizedBox(height: 6),
              _schedRow('Exit ', '1:00 AM IST', '19:30 UTC', AppColors.red),
            ],
          ]),
        ),
      ),
    );
  }

  Widget _statusPill(bool open) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 5),
    decoration: BoxDecoration(
      border: Border.all(color: open ? AppColors.cyan : AppColors.sub),
      borderRadius: BorderRadius.circular(20),
      color: open ? AppColors.cyan.withOpacity(0.1) : Colors.transparent,
      boxShadow: open
          ? [BoxShadow(color: AppColors.cyan.withOpacity(0.2), blurRadius: 10, spreadRadius: 1)]
          : null,
    ),
    child: Row(mainAxisSize: MainAxisSize.min, children: [
      Container(
        width: 7, height: 7,
        decoration: BoxDecoration(color: open ? AppColors.cyan : AppColors.sub, shape: BoxShape.circle),
      ),
      const SizedBox(width: 6),
      Text(
        open ? 'OPEN' : 'IDLE',
        style: TextStyle(
          color: open ? AppColors.cyan : AppColors.sub,
          fontSize: 11, letterSpacing: 1.5, fontWeight: FontWeight.w800,
        ),
      ),
    ]),
  );

  Widget _posRow(String label, String value, {Color? color}) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 5),
    child: Row(children: [
      Text(label, style: const TextStyle(color: AppColors.sub, fontSize: 13)),
      const Spacer(),
      Flexible(
        child: Text(
          value,
          style: TextStyle(
            color: color ?? AppColors.text, fontSize: 13, fontWeight: FontWeight.w600,
            fontFeatures: const [FontFeature.tabularFigures()],
          ),
          overflow: TextOverflow.ellipsis,
        ),
      ),
    ]),
  );

  Widget _schedRow(String label, String ist, String utc, Color c) => Row(children: [
    Text(label, style: const TextStyle(color: AppColors.sub, fontSize: 13, fontFamily: 'monospace')),
    const SizedBox(width: 10),
    Text(ist, style: TextStyle(color: c, fontSize: 13, fontWeight: FontWeight.w600)),
    const SizedBox(width: 8),
    Text(utc, style: const TextStyle(color: AppColors.sub, fontSize: 12)),
  ]);

  Widget _pnlRow(double? pnl) {
    if (pnl == null) return const SizedBox.shrink();
    final pos = pnl >= 0;
    final color = pos ? AppColors.green : AppColors.red;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 11),
      decoration: BoxDecoration(
        color: color.withOpacity(0.08),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: color.withOpacity(0.3)),
      ),
      child: Row(children: [
        Text('Live P&L', style: TextStyle(color: color, fontSize: 13, fontWeight: FontWeight.w600)),
        const Spacer(),
        Text(
          '${pos ? '+' : ''}\$${pnl.toStringAsFixed(2)}',
          style: TextStyle(color: color, fontSize: 20, fontWeight: FontWeight.w900,
              fontFeatures: const [FontFeature.tabularFigures()]),
        ),
      ]),
    );
  }

  // ── Stats Grid ───────────────────────────────────────────────
  Widget _buildStatsGrid(Summary? s) {
    if (s == null) return const SizedBox(height: 6);
    final items = [
      _SI('Total P&L',    _fmtPnl(s.totalPnl),                AppColors.gold),
      _SI('Win Rate',     '${s.winRate.toStringAsFixed(1)}%',  AppColors.cyan),
      _SI('Avg Win',      '+\$${s.avgWin.toStringAsFixed(0)}', AppColors.green),
      _SI('Avg Loss',     '\$${s.avgLoss.toStringAsFixed(0)}', AppColors.red),
      _SI('Reward/Risk',  '${s.rr.toStringAsFixed(2)}×',       AppColors.blue),
      _SI('Max DD',       '\$${s.maxDd.abs().toStringAsFixed(0)}', AppColors.red),
    ];
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 6, 14, 6),
      child: GridView.count(
        crossAxisCount: 2,
        shrinkWrap: true,
        physics: const NeverScrollableScrollPhysics(),
        crossAxisSpacing: 10,
        mainAxisSpacing: 10,
        childAspectRatio: 2.2,
        children: items.map((e) => _StatCard(item: e)).toList(),
      ),
    );
  }

  // ── Chart ────────────────────────────────────────────────────
  Widget _buildChart(List<Trade> trades) {
    final spots = trades.asMap().entries
        .map((e) => FlSpot(e.key.toDouble(), e.value.cumPnl))
        .toList();
    final maxY = (trades.map((t) => t.cumPnl).reduce((a, b) => a > b ? a : b) / 10000).ceil() * 10000.0;
    final minY = (trades.map((t) => t.cumPnl).reduce((a, b) => a < b ? a : b) / 5000).floor() * 5000.0;

    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 6, 14, 6),
      child: Card(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(8, 16, 16, 8),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            const Padding(
              padding: EdgeInsets.only(left: 8),
              child: Text('Cumulative P&L',
                  style: TextStyle(color: AppColors.sub, fontSize: 10, letterSpacing: 2)),
            ),
            const SizedBox(height: 14),
            SizedBox(
              height: 190,
              child: LineChart(LineChartData(
                minY: minY,
                maxY: maxY,
                gridData: FlGridData(
                  show: true,
                  drawVerticalLine: false,
                  horizontalInterval: 20000,
                  getDrawingHorizontalLine: (_) =>
                      const FlLine(color: AppColors.border, strokeWidth: 1),
                ),
                titlesData: FlTitlesData(
                  leftTitles: AxisTitles(sideTitles: SideTitles(
                    showTitles: true,
                    reservedSize: 48,
                    interval: 20000,
                    getTitlesWidget: (v, _) => Text(
                      '\$${(v / 1000).toStringAsFixed(0)}k',
                      style: const TextStyle(color: AppColors.sub, fontSize: 9),
                    ),
                  )),
                  bottomTitles: AxisTitles(sideTitles: SideTitles(showTitles: false)),
                  rightTitles: AxisTitles(sideTitles: SideTitles(showTitles: false)),
                  topTitles:   AxisTitles(sideTitles: SideTitles(showTitles: false)),
                ),
                borderData: FlBorderData(show: false),
                lineTouchData: LineTouchData(
                  touchTooltipData: LineTouchTooltipData(
                    tooltipBgColor: AppColors.card,
                    getTooltipItems: (spots) => spots.map((s) => LineTooltipItem(
                      '\$${s.y.toStringAsFixed(0)}',
                      const TextStyle(color: AppColors.gold, fontWeight: FontWeight.bold, fontSize: 12),
                    )).toList(),
                  ),
                ),
                lineBarsData: [
                  LineChartBarData(
                    spots: spots,
                    isCurved: true,
                    curveSmoothness: 0.3,
                    color: AppColors.gold,
                    barWidth: 2.5,
                    dotData: const FlDotData(show: false),
                    belowBarData: BarAreaData(
                      show: true,
                      gradient: LinearGradient(
                        begin: Alignment.topCenter,
                        end: Alignment.bottomCenter,
                        colors: [
                          AppColors.gold.withOpacity(0.18),
                          AppColors.gold.withOpacity(0.02),
                        ],
                      ),
                    ),
                  ),
                ],
              )),
            ),
            const SizedBox(height: 6),
            Padding(
              padding: const EdgeInsets.only(left: 8),
              child: Text(
                '${trades.length} trades  ·  '
                '${trades.where((t) => t.isWin).length}W / '
                '${trades.where((t) => !t.isWin).length}L',
                style: const TextStyle(color: AppColors.sub, fontSize: 10),
              ),
            ),
          ]),
        ),
      ),
    );
  }

  String _comma(double v) => NumberFormat('#,##0').format(v);
  String _fmtPnl(double v) =>
      (v >= 0 ? '+\$' : '-\$') + NumberFormat('#,##0').format(v.abs());
}

class _SI {
  final String label, value;
  final Color accent;
  const _SI(this.label, this.value, this.accent);
}

class _StatCard extends StatelessWidget {
  final _SI item;
  const _StatCard({super.key, required this.item});

  @override
  Widget build(BuildContext context) => Card(
    child: Container(
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(12),
        border: Border(top: BorderSide(color: item.accent, width: 3)),
      ),
      padding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Text(item.label,
              style: const TextStyle(color: AppColors.sub, fontSize: 10, letterSpacing: 1.5)),
          const SizedBox(height: 5),
          Text(item.value,
              style: TextStyle(
                  color: item.accent, fontSize: 22, fontWeight: FontWeight.w900,
                  fontFeatures: const [FontFeature.tabularFigures()])),
        ],
      ),
    ),
  );
}
