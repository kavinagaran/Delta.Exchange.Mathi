/* NITHI-BOT dashboard — shared helpers + topbar live data */

async function jget(url) {
  const r = await fetch(url);
  if (r.status === 401) { location.href = '/login'; throw new Error('unauthenticated'); }
  return r.json();
}

async function jpost(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { location.href = '/login'; throw new Error('unauthenticated'); }
  return r.json();
}

/* formatting */
const fN = (v, d = 0) => (v == null || isNaN(+v)) ? '—' : (+v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const f$ = v => (v == null || isNaN(+v)) ? '—' : (v < 0 ? '-$' : '+$') + Math.abs(+v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pnlCls = v => v == null ? 'c-muted' : (+v >= 0 ? 'c-pos' : 'c-neg');
const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function toast(msg, type = 'ok') {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = `toast ${type} show`;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, 3600);
}

/* IST helpers */
const IST_OFFSET_MIN = 330;
function istNowMinutes() {
  const n = new Date();
  return (n.getUTCHours() * 60 + n.getUTCMinutes() + IST_OFFSET_MIN) % 1440;
}
function utcToIst(hhmmss) {
  if (!hhmmss) return '';
  const [h, m] = hhmmss.split(':').map(Number);
  const t = (h * 60 + m + IST_OFFSET_MIN) % 1440;
  const hh = Math.floor(t / 60), mm = t % 60;
  const ap = hh >= 12 ? 'PM' : 'AM';
  return `${((hh + 11) % 12) + 1}:${String(mm).padStart(2, '0')} ${ap} IST`;
}

/* ── Topbar: BTC price, balance, status pill, clock ─────── */
function _setPill(el, cls, text) {
  el.className = `pill ${cls}`;
  el.innerHTML = `<span class="dot"></span>${esc(text)}`;
}

function statusFromSlots(st) {
  const slots = [st.morning || {}, { ...st, morning: undefined, trend: undefined }, st.trend || {}];
  const open = slots.filter(s => s && s.status === 'OPEN');
  const realOpen = open.filter(s => !s.dry_run);
  if (realOpen.length) {
    const pnl = realOpen.reduce((a, s) => a + (+s.live_pnl || 0), 0);
    return { cls: 'live', text: `LIVE ${f$(pnl)}`, pnl };
  }
  if (open.length) return { cls: 'sim', text: 'SIMULATED ONLY' };
  const closed = slots.filter(s => s && s.status === 'CLOSED' && !s.dry_run);
  if (closed.length) {
    const last = closed.reduce((a, b) => ((a.exit_time_utc || '') > (b.exit_time_utc || '') ? a : b));
    const pnl = +last.pnl_usd || 0;
    return { cls: pnl >= 0 ? 'closed-profit' : 'closed-loss', text: `CLOSED ${f$(pnl)}`, pnl };
  }
  return { cls: 'idle', text: 'IDLE — waiting' };
}

async function refreshTopbar() {
  try {
    const st = await jget('/api/status');
    const btc = document.getElementById('tb-btc');
    if (btc) {
      const price = +st.btc_futures_price;
      const previous = window._lastBtcPrice;
      const valid = Number.isFinite(price) && price > 0;
      btc.classList.remove('btc-up', 'btc-down');
      if (valid && Number.isFinite(previous)) {
        if (price > previous) btc.classList.add('btc-up');
        else if (price < previous) btc.classList.add('btc-down');
      }
      if (valid) window._lastBtcPrice = price;
      btc.innerHTML = `BTC <b>$${fN(valid ? price : null)}</b>`;
    }
    const pill = document.getElementById('tb-pill');
    if (pill) {
      const s = statusFromSlots(st);
      _setPill(pill, s.cls, s.text);
    }
    window._lastStatus = st;
    document.dispatchEvent(new CustomEvent('status', { detail: st }));
  } catch (e) { /* transient */ }
  try {
    const w = await jget('/api/wallet');
    const el = document.getElementById('tb-bal');
    if (el && w.usd_balance != null) {
      const inr = w.inr_balance != null ? ` · ₹${fN(w.inr_balance)}` : '';
      el.innerHTML = `Balance <b>$${fN(w.usd_balance, 2)}</b>${inr}`;
    }
  } catch (e) { /* transient */ }
}

function tickClock() {
  const el = document.getElementById('tb-clock');
  if (!el) return;
  const now = new Date();
  const ist = new Date(now.getTime() + IST_OFFSET_MIN * 60000);
  el.innerHTML = `<b>${ist.toUTCString().slice(17, 22)}</b> IST · ${now.toUTCString().slice(17, 22)} UTC`;
}

document.addEventListener('DOMContentLoaded', () => {
  tickClock();
  setInterval(tickClock, 10_000);
  refreshTopbar();
  setInterval(refreshTopbar, 10_000);
});
