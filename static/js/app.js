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

/* ── Persistent site theme ──────────────────────────────── */
const THEME_STORAGE_KEY = 'nithi-theme';

function currentTheme() {
  return document.documentElement?.dataset?.theme === 'dark' ? 'dark' : 'light';
}

function applyTheme(theme, persist = true) {
  const next = theme === 'dark' ? 'dark' : 'light';
  if (next === 'dark') document.documentElement.dataset.theme = 'dark';
  else delete document.documentElement.dataset.theme;
  if (persist) {
    try { localStorage.setItem(THEME_STORAGE_KEY, next); } catch (_) { /* storage unavailable */ }
  }
  document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: next } }));
  return next;
}

function initThemeToggle() {
  const toggle = document.getElementById('theme-toggle');
  if (!toggle) return;
  const sync = () => {
    const dark = currentTheme() === 'dark';
    toggle.setAttribute('aria-pressed', String(dark));
    toggle.setAttribute('aria-label', `Switch to ${dark ? 'light' : 'dark'} theme`);
    toggle.title = `Switch to ${dark ? 'light' : 'dark'} theme`;
  };
  sync();
  toggle.addEventListener('click', () => {
    applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
    sync();
  });
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

function _closedPill(trade) {
  const raw = trade && trade.pnl_usd;
  const known = raw !== null && raw !== undefined && raw !== '' && Number.isFinite(Number(raw));
  if (!known) return { cls: 'closed', text: 'CLOSED —', pnl: null };
  const pnl = Number(raw);
  const cls = pnl > 0 ? 'closed-profit' : (pnl < 0 ? 'closed-loss' : 'closed');
  return { cls, text: `CLOSED ${f$(pnl)}`, pnl };
}

function _closedAtMs(trade) {
  if (!trade) return Number.NEGATIVE_INFINITY;
  const explicit = trade.closed_at_utc || trade.exit_at_utc;
  if (explicit) {
    const parsed = Date.parse(explicit);
    if (Number.isFinite(parsed)) return parsed;
  }
  const clock = trade.exit_time_utc || trade.exit_time || '';
  const date = trade.exit_date || trade.entry_date || trade.date || '';
  if (!date || !clock) return Number.NEGATIVE_INFINITY;
  let parsed = Date.parse(`${date}T${clock}Z`);
  const entryClock = trade.entry_time_utc || trade.entry_time || '';
  if (!trade.exit_date && entryClock && clock < entryClock) parsed += 86_400_000;
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
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
  if (st.latest_closed_trade) return _closedPill(st.latest_closed_trade);
  const closed = slots.filter(s => s && s.status === 'CLOSED' && !s.dry_run);
  if (closed.length) {
    const last = closed.reduce((a, b) => (_closedAtMs(a) > _closedAtMs(b) ? a : b));
    return _closedPill(last);
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
  initThemeToggle();
  tickClock();
  setInterval(tickClock, 10_000);
  refreshTopbar();
  setInterval(refreshTopbar, 10_000);
});
