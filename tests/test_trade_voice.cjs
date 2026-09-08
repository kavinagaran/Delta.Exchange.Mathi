const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
function setup() {
  let now = 0, cancels = 0;
  const spoken = [], events = {}, buttons = [];
  const context = {
    window: {speechSynthesis: {speak: u => spoken.push(u.text), cancel: () => cancels++}},
    document: {addEventListener: (e, fn) => events[e] = fn,
      createElement: () => ({style: {}}), body: {appendChild: b => buttons.push(b)}},
    SpeechSynthesisUtterance: function(text) { this.text = text; },
    Date: {now: () => now},
    jget: async () => [],
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/js/trade_voice.js'), 'utf8'), context);
  const api = context.window.tradeVoiceAnnouncements;
  events.DOMContentLoaded();
  api.setEnabled(true); buttons[0].onclick(); spoken.length = 0;
  return {api, spoken, events, context, cancels: () => cancels, tick: () => now += 900000};
}
const open = (id = 'a') => ({position_cycle_id: id, symbol: 'C-BTC', status: 'OPEN', lots: 250, live_pnl: 12.5});
test('baseline is silent; entry and 15-minute P&L are spoken', () => {
  const s = setup(); s.api.observe([]); s.api.observe([open()]);
  assert.match(s.spoken[0], /Trade taken.*250 lots/);
  s.api.observe([open()]); assert.equal(s.spoken.length, 1);
  s.tick(); s.api.observe([open()]); assert.match(s.spoken[1], /profit of 12.50 dollars/);
});
test('OFF cancels speech and ON does not replay trades', () => {
  const s = setup(); s.api.observe([]); s.api.setEnabled(false);
  assert.ok(s.cancels() > 0);
  s.api.observe([open()]); s.tick(); s.api.observe([open()]);
  s.api.setEnabled(true); s.api.observe([open()]);
  assert.deepEqual(s.spoken, []);
});
test('missing data waits for booked history, null is not zero', () => {
  const s = setup(); s.api.observe([open()]); s.api.observe(null); s.api.observe([]);
  const closed = {...open(), status: 'CLOSED', live_pnl: 100, pnl_usd: null};
  s.api.observe([closed]); assert.deepEqual(s.spoken, []);
  s.api.observe([{...closed, pnl_usd: -5.25}]);
  assert.match(s.spoken[0], /Booked loss of 5.25 dollars/);
  s.api.observe([{...closed, pnl_usd: -5.25}]); assert.equal(s.spoken.length, 1);
});
test('exit then entry does not cancel either announcement', () => {
  const s = setup(); s.api.observe([open()]);
  s.api.observe([{...open(), status: 'CLOSED', pnl_usd: 8}, open('b')]);
  assert.match(s.spoken[0], /Trade closed/); assert.match(s.spoken[1], /Trade taken/);
  assert.equal(s.cancels(), 0);
});
test('preference change discards in-flight trade fetch', async () => {
  const s = setup(); let resolve;
  s.context.jget = () => new Promise(r => resolve = r);
  const pending = s.events.status({detail: {voice_announcements_enabled: true, dry_run_mode: false}});
  s.api.setEnabled(false); resolve([open()]); await pending;
  assert.deepEqual(s.spoken, []);
});
