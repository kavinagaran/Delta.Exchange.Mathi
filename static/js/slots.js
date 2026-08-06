/* Shared position/slot helpers — single source for logic that was duplicated
 * between overview.html (live*) and dry_run.html (dry*).
 * Plain globals, no modules: the Flutter WebView and asset_v() cache-busting
 * depend on classic scripts. Requires app.js (utcToIst) to be loaded first. */

/* IST calendar routing for MOVE positions: entries before 11:00 IST belong to
 * the Morning card, later ones to Evening. Identical rule for live and dry run. */
function moveDisplaySlotFromUtc(entryTimeUtc, fallback = 'evening') {
  const raw = String(entryTimeUtc || '').trim();
  let hour;
  let minute;
  if (raw.includes('T') || raw.includes(' ')) {
    const parsed = new Date(raw.endsWith('Z') || /[+-]\d\d:\d\d$/.test(raw)
      ? raw : raw + 'Z');
    if (Number.isFinite(parsed.getTime())) {
      hour = parsed.getUTCHours();
      minute = parsed.getUTCMinutes();
    }
  } else {
    const parts = raw.split(':').map(Number);
    if (parts.length >= 2 && parts.every(Number.isFinite)) {
      [hour, minute] = parts;
    }
  }
  if (!Number.isFinite(hour) || !Number.isFinite(minute)) {
    return fallback === 'morning' ? 'morning' : 'evening';
  }
  const istMinutes = (hour * 60 + minute + 330) % 1440;
  return istMinutes < 11 * 60 ? 'morning' : 'evening';
}

/* HH:MM:SS UTC entry clock for a state record. Prefers the explicit
 * entry_at_utc timestamp, falls back to the plain clock fields. */
function entryUtcClock(state) {
  const explicit = String(state?.entry_at_utc || '').trim();
  if (explicit) {
    const entered = new Date(
      explicit.endsWith('Z') || /[+-]\d\d:\d\d$/.test(explicit)
        ? explicit : explicit + 'Z',
    );
    if (Number.isFinite(entered.getTime())) {
      return [
        entered.getUTCHours(),
        entered.getUTCMinutes(),
        entered.getUTCSeconds(),
      ].map(value => String(value).padStart(2, '0')).join(':');
    }
  }
  const clock = String(state?.entry_time_utc || state?.entry_time || '').trim();
  if (clock && !clock.includes('T') && !clock.includes(' ')) return clock;
  if (!clock) return '';
  const parsed = new Date(clock.endsWith('Z') || /[+-]\d\d:\d\d$/.test(clock)
    ? clock : clock + 'Z');
  if (!Number.isFinite(parsed.getTime())) return '';
  return [parsed.getUTCHours(), parsed.getUTCMinutes(), parsed.getUTCSeconds()]
    .map(value => String(value).padStart(2, '0')).join(':');
}

function tradeTimeIst(state) {
  return utcToIst(entryUtcClock(state)) || '—';
}

/* Which dashboard card a position belongs on, from its instrument:
 * vanilla CE/PE → trend; MOVE → morning/evening by IST entry time. */
function instrumentDisplaySlot(state, fallback = 'evening') {
  const symbol = String(state?.symbol || '').toUpperCase();
  const instrument = String(state?.instrument_kind || '').toUpperCase();
  const optionType = String(state?.option_type || '').toUpperCase();
  if (instrument === 'BTC_OPTION' || ['CE', 'PE', 'CALL', 'PUT'].includes(optionType)
      || symbol.startsWith('C-BTC') || symbol.startsWith('P-BTC')) {
    return 'trend';
  }
  if (instrument === 'BTC_MOVE' || optionType === 'MOVE' || symbol.startsWith('MV-BTC')) {
    return moveDisplaySlotFromUtc(
      state?.entry_at_utc || state?.entry_time_utc || state?.entry_time,
      fallback,
    );
  }
  return ['morning', 'evening', 'trend'].includes(fallback)
    ? fallback : 'evening';
}
