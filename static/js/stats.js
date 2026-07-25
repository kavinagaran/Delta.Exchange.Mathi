/* Shared stat-tile renderer — previously byte-identical loadStats() copies in
 * overview.html and trades.html. Requires app.js (jget, fN, f$, pnlCls). */

async function loadStatsInto(elementId = 'stats', endpoint = '/api/summary') {
  const s = await jget(endpoint);
  const el = document.getElementById(elementId);
  if (!el) return;
  if (!s || !s.total_days) { el.innerHTML = ''; return; }
  el.innerHTML = `
    <div class="stat"><div class="lbl">Total P&L</div><div class="val ${pnlCls(s.total_pnl)}">${f$(s.total_pnl)}</div><div class="sub">${s.total_days} trades</div></div>
    <div class="stat"><div class="lbl">Win rate</div><div class="val">${fN(s.win_rate, 1)}%</div><div class="sub">${s.wins} W / ${s.losses} L</div></div>
    <div class="stat"><div class="lbl">Avg win</div><div class="val c-pos">${f$(s.avg_win)}</div><div class="sub">Avg loss ${f$(s.avg_loss)}</div></div>
    <div class="stat"><div class="lbl">Risk / reward</div><div class="val">${fN(s.rr, 2)}</div><div class="sub">Reward per $1 risked</div></div>
    <div class="stat"><div class="lbl">Max drawdown</div><div class="val c-neg">${f$(s.max_dd)}</div><div class="sub">Peak to trough</div></div>`;
}
