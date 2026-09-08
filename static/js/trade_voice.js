/* Presentation-only voice alerts. Never changes orders or protection. */
(function () {
  const intervalMs = 15 * 60 * 1000;
  const audioSessionKey = 'btc-bot-voice-audio-enabled';
  let enabled = false, initialized = false, unlocked = false, busy = false;
  let lastPnlAt = 0, mode = null, revision = 0;
  let active = new Map(), pending = new Set();
  let audioButton;
  const isOpen = t => t && (t._live === true || String(t.status).toUpperCase() === 'OPEN');
  const id = t => String(t.position_cycle_id || t.simulation_id || t.trade_id ||
    [t.symbol, t.entry_date || t.date, t.entry_time_utc || t.entry_time].join('|'));
  function pnl(t) {
    for (const key of isOpen(t) ? ['live_pnl', 'pnl_usd', 'net_pnl', 'gross_pnl_usd'] : ['pnl_usd', 'net_pnl', 'gross_pnl_usd']) {
      const raw = t?.[key];
      if (raw == null || raw === '' || typeof raw === 'boolean') continue;
      const n = Number(raw);
      if (Number.isFinite(n)) return n;
    }
    return null;
  }
  const dollars = n => `${n >= 0 ? 'profit' : 'loss'} of ${Math.abs(n).toFixed(2)} dollars`;
  try { unlocked = sessionStorage.getItem(audioSessionKey) === 'true'; } catch (_) {}
  function rememberUnlock(value) {
    unlocked = value;
    try {
      if (value) sessionStorage.setItem(audioSessionKey, 'true');
      else sessionStorage.removeItem(audioSessionKey);
    } catch (_) { /* storage may be unavailable */ }
    updateButton();
  }
  function speak(text, force = false) {
    if ((!enabled && !force) || !unlocked || !window.speechSynthesis) return false;
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'en-US';
    utterance.onerror = e => {
      if (e.error === 'not-allowed') rememberUnlock(false);
    };
    // Keep the native queue: exits must not be interrupted by a new entry.
    window.speechSynthesis.speak(utterance);
    return true;
  }
  function test() {
    if (!window.speechSynthesis) return false;
    // This is called directly by a click, satisfying browser user-gesture
    // policies. Remember the choice for same-tab page navigation; if a
    // browser rejects it later, onerror restores the activation button.
    rememberUnlock(true);
    return speak('Voice announcements are working.', true);
  }
  function updateButton() {
    if (audioButton) audioButton.hidden = !enabled || unlocked;
  }
  function setEnabled(value) {
    const next = value === true;
    if (next !== enabled) {
      revision++;
      enabled = next;
      initialized = false;
      active.clear(); pending.clear();
    }
    if (!enabled) window.speechSynthesis?.cancel();
    updateButton();
  }
  function observe(rows) {
    if (!Array.isArray(rows)) return; // failed requests are not exits
    const trades = rows.filter(t => t && typeof t === 'object');
    const open = new Map(trades.filter(isOpen).map(t => [id(t), t]));
    const now = Date.now();
    if (!enabled || !initialized) {
      initialized = true; active = open; pending.clear(); lastPnlAt = now;
      return; // never replay history when enabling or opening a page
    }
    for (const key of active.keys()) if (!open.has(key)) pending.add(key);
    for (const t of trades) {
      const key = id(t), booked = pnl(t);
      if (!isOpen(t) && pending.has(key) && booked !== null) {
        speak(`Trade closed. ${t.symbol || 'Option trade'}. Booked ${dollars(booked)}.`);
        pending.delete(key);
      }
    }
    for (const [key, t] of open) {
      if (!active.has(key) && !pending.has(key)) {
        speak(`Trade taken. ${t.symbol || 'Option trade'}, ${t.side || ''}, ${t.lots || 0} lots.`);
        lastPnlAt = now;
      }
      pending.delete(key);
    }
    active = open;
    if (now - lastPnlAt >= intervalMs) {
      for (const t of open.values()) {
        const value = pnl(t);
        if (value !== null) speak(`Current trade ${t.symbol || ''}. ${dollars(value)}.`);
      }
      lastPnlAt = now;
    }
  }
  document.addEventListener('DOMContentLoaded', () => {
    if (!window.speechSynthesis) return;
    audioButton = document.createElement('button');
    audioButton.type = 'button';
    audioButton.className = 'btn secondary';
    audioButton.textContent = 'Enable voice audio';
    audioButton.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:900';
    audioButton.onclick = test;
    document.body.appendChild(audioButton);
    updateButton();
  });
  // Shared status polling keeps voice active on every dashboard page.
  document.addEventListener('status', async e => {
    const st = e.detail;
    if (typeof st?.voice_announcements_enabled !== 'boolean') return;
    setEnabled(st.voice_announcements_enabled);
    if (mode !== st.dry_run_mode) {
      mode = st.dry_run_mode; initialized = false; revision++;
      window.speechSynthesis?.cancel();
    }
    if (!enabled || busy) return;
    busy = true;
    const version = revision;
    try {
      const rows = await jget('/api/today-trades');
      if (version === revision) observe(rows);
    } catch (_) { /* wait for authoritative trade data */ }
    finally { busy = false; }
  });
  window.tradeVoiceAnnouncements = { observe, setEnabled, test };
})();
