/* Shared TP/SL/TSL protection-config helpers. One key map instead of the two
 * that previously lived in overview.html (saveTp) and dry_run.html
 * (saveDryProtection). Requires app.js (jpost, toast). */

function protectionConfigKeys(controlSlot) {
  if (controlSlot === 'morning') {
    return { tp: 'TP_TARGET_PNL_MORNING', sl: 'SL_TARGET_PNL_MORNING',
             arm: 'TSL_ARM_PNL_MORNING', trail: 'TSL_TRAIL_PNL_MORNING',
             lock: 'TSL_LOCK_MIN_PNL_MORNING', poll: 'TP_POLL_SECS_MORNING' };
  }
  if (controlSlot === 'trend') {
    return { tp: 'TP_TARGET_PNL_TREND', sl: 'SL_TARGET_PNL_TREND',
             arm: 'TSL_ARM_PNL_TREND', trail: 'TSL_TRAIL_PNL_TREND',
             lock: 'TSL_LOCK_MIN_PNL_TREND', poll: 'TP_POLL_SECS_TREND' };
  }
  return { tp: 'TP_TARGET_PNL', sl: 'SL_TARGET_PNL',
           arm: 'TSL_ARM_PNL', trail: 'TSL_TRAIL_PNL',
           lock: 'TSL_LOCK_MIN_PNL', poll: 'TP_POLL_SECS' };
}

/* values: {tp, sl, arm, trail, lock, poll} as numbers.
 * Returns an error string, or '' when valid. */
function protectionValuesError(values) {
  if (!Number.isFinite(values.tp) || values.tp < 1 ||
      !Number.isFinite(values.poll) || values.poll < 10 ||
      ['sl', 'arm', 'trail', 'lock'].some(
        key => !Number.isFinite(values[key]) || values[key] < 0)) {
    return 'Enter TP of at least $1, poll of at least 10 seconds, and '
      + 'non-negative SL/TSL values';
  }
  return '';
}

/* Read the six protection inputs for a slot, given an element-id prefix
 * (`tp` on Overview → tp-target-…; `dry` on Paper → dry-tp-…). */
function readProtectionInputs(idFor) {
  const read = id => Number(document.getElementById(id)?.value);
  return {
    tp: read(idFor.tp), sl: read(idFor.sl), arm: read(idFor.arm),
    trail: read(idFor.trail), lock: read(idFor.lock), poll: read(idFor.poll),
  };
}

/* Persist one slot's protection values through /api/config.
 * Returns the API result (caller owns toasts/refresh). */
async function saveProtectionConfig(controlSlot, values) {
  const keys = protectionConfigKeys(controlSlot);
  const error = protectionValuesError(values);
  if (error) {
    toast(error, 'err');
    return { ok: false, error };
  }
  return jpost('/api/config', {
    [keys.tp]: values.tp, [keys.sl]: values.sl, [keys.arm]: values.arm,
    [keys.trail]: values.trail, [keys.lock]: values.lock,
    [keys.poll]: values.poll,
  });
}
