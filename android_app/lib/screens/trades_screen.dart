import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';
import '../models/trade.dart';
import '../services/api_service.dart';
import '../theme.dart';

class TradesScreen extends StatelessWidget {
  const TradesScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Consumer<ApiService>(builder: (ctx, api, _) {
      final trades = api.trades.reversed.toList();
      return Scaffold(
        appBar: AppBar(
          title: Text(
            trades.isEmpty ? 'Trade History' : 'Trade History  (${trades.length})',
            style: const TextStyle(color: AppColors.text),
          ),
          actions: [
            IconButton(
              icon: const Icon(Icons.download_for_offline_outlined, color: AppColors.cyan),
              tooltip: 'Import Backtest',
              onPressed: () => _importBacktest(context, api),
            ),
          ],
        ),
        body: trades.isEmpty
            ? Center(
                child: Column(mainAxisSize: MainAxisSize.min, children: [
                  Icon(Icons.receipt_long_outlined, color: AppColors.sub, size: 52),
                  const SizedBox(height: 12),
                  Text(
                    api.error != null ? 'Cannot reach server' : 'No trades yet',
                    style: const TextStyle(color: AppColors.sub, fontSize: 15),
                  ),
                  if (api.error == null)
                    const Padding(
                      padding: EdgeInsets.only(top: 6),
                      child: Text('Tap ⬇ to import 2026 backtest data',
                          style: TextStyle(color: AppColors.sub, fontSize: 12)),
                    ),
                ]),
              )
            : RefreshIndicator(
                color: AppColors.cyan,
                backgroundColor: AppColors.card,
                onRefresh: api.refresh,
                child: ListView.builder(
                  padding: const EdgeInsets.fromLTRB(12, 8, 12, 20),
                  itemCount: trades.length,
                  itemBuilder: (_, i) => _TradeTile(trade: trades[i]),
                ),
              ),
      );
    });
  }

  Future<void> _importBacktest(BuildContext ctx, ApiService api) async {
    final sm = ScaffoldMessenger.of(ctx);
    try {
      sm.showSnackBar(const SnackBar(
        content: Text('Importing backtest data...'),
        duration: Duration(seconds: 2),
      ));
      final res = await api.importBacktest();
      await api.refresh();
      sm.showSnackBar(SnackBar(
        content: Text('Imported ${res['added']} trades  (total: ${res['total']})'),
        backgroundColor: AppColors.green.withOpacity(0.9),
      ));
    } catch (e) {
      sm.showSnackBar(SnackBar(
        content: Text('Import failed: $e'),
        backgroundColor: AppColors.red.withOpacity(0.9),
      ));
    }
  }
}

class _TradeTile extends StatelessWidget {
  final Trade trade;
  const _TradeTile({required this.trade});

  @override
  Widget build(BuildContext context) {
    final win  = trade.isWin;
    final mvUp = trade.btcMovePct >= 0;
    final accent = win ? AppColors.green : AppColors.red;

    return Card(
      margin: const EdgeInsets.only(bottom: 9),
      child: Container(
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(12),
          border: Border(left: BorderSide(color: accent, width: 3)),
        ),
        padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
        child: Column(children: [
          // Row 1 — date + badges + P&L
          Row(children: [
            Text(trade.date,
                style: const TextStyle(color: AppColors.sub, fontSize: 12,
                    fontFeatures: [FontFeature.tabularFigures()])),
            const SizedBox(width: 8),
            _badge(win ? 'WIN' : 'LOSS', accent),
            if (trade.isSim) ...[const SizedBox(width: 6), _badge('SIM', AppColors.sub)],
            const Spacer(),
            Text(
              '${win ? '+' : ''}\$${trade.pnlUsd.toStringAsFixed(2)}',
              style: TextStyle(
                color: accent, fontSize: 17, fontWeight: FontWeight.w900,
                fontFeatures: const [FontFeature.tabularFigures()],
              ),
            ),
          ]),
          const SizedBox(height: 10),
          // Row 2 — details
          Row(children: [
            Expanded(child: _col('SYMBOL', trade.symbol, AppColors.cyan)),
            Expanded(child: _col('BTC', '\$${NumberFormat('#,##0').format(trade.btcEntry)}')),
            Expanded(child: _col(
              'MOVE',
              '${mvUp ? '▲' : '▼'}${trade.btcMovePct.abs().toStringAsFixed(2)}%',
              mvUp ? AppColors.green : AppColors.red,
            )),
            Expanded(child: _col(
              'CUM',
              '${trade.cumPnl >= 0 ? '+' : ''}\$${NumberFormat('#,##0').format(trade.cumPnl)}',
              AppColors.gold,
            )),
          ]),
          if (trade.entryTime.isNotEmpty || trade.exitTime.isNotEmpty) ...[
            const SizedBox(height: 8),
            Row(children: [
              _infoChip(Icons.login_rounded, trade.entryTime, AppColors.sub),
              const SizedBox(width: 8),
              _infoChip(Icons.logout_rounded, trade.exitTime, AppColors.sub),
            ]),
          ],
        ]),
      ),
    );
  }

  Widget _badge(String label, Color c) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
    decoration: BoxDecoration(
      color: c.withOpacity(0.12),
      borderRadius: BorderRadius.circular(4),
    ),
    child: Text(label,
        style: TextStyle(color: c, fontSize: 10, fontWeight: FontWeight.w800, letterSpacing: 1)),
  );

  Widget _col(String lbl, String val, [Color? c]) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text(lbl, style: const TextStyle(color: AppColors.sub, fontSize: 9, letterSpacing: 1.5)),
      const SizedBox(height: 3),
      Text(val,
          style: TextStyle(color: c ?? AppColors.text, fontSize: 12, fontWeight: FontWeight.w700),
          overflow: TextOverflow.ellipsis),
    ],
  );

  Widget _infoChip(IconData icon, String label, Color c) => Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      Icon(icon, size: 11, color: c),
      const SizedBox(width: 4),
      Text(label, style: TextStyle(color: c, fontSize: 10)),
    ],
  );
}
