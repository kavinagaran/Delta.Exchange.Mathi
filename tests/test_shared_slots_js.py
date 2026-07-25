"""static/js/slots.js — shared slot-routing helpers.

Added in Phase 2 as an additive extraction but left unreferenced by any page
until UI-4 wired overview.html's loadToday() to call instrumentDisplaySlot()
directly (previously each page kept its own copy: liveDisplaySlot in
overview.html, a differently-ordered dryDisplaySlot equivalent in
dry_run.html). This is the first direct test of the shared implementation
now that a real page depends on it.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_move_display_slot_routes_by_actual_ist_trade_time():
    script = r"""
const fs = require('fs');
const vm = require('vm');
vm.runInThisContext(fs.readFileSync('static/js/slots.js', 'utf8'));

if (moveDisplaySlotFromUtc('05:29:59') !== 'morning') {
  throw new Error('10:59:59 AM IST did not route to Morning');
}
if (moveDisplaySlotFromUtc('05:30:00') !== 'evening') {
  throw new Error('11:00 AM IST did not route to Evening');
}
if (moveDisplaySlotFromUtc('2026-07-23T01:50:00Z') !== 'morning') {
  throw new Error('full ISO timestamp before the boundary did not route to Morning');
}
if (moveDisplaySlotFromUtc('', 'morning') !== 'morning') {
  throw new Error('unparseable input did not fall back to the caller-supplied default');
}
if (moveDisplaySlotFromUtc('', 'evening') !== 'evening') {
  throw new Error('unparseable input ignored a non-morning fallback');
}
"""
    result = subprocess.run([NODE, "-e", script], cwd=ROOT, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_instrument_display_slot_distinguishes_options_from_move_from_unknown():
    script = r"""
const fs = require('fs');
const vm = require('vm');
vm.runInThisContext(fs.readFileSync('static/js/slots.js', 'utf8'));

if (instrumentDisplaySlot({ symbol: 'C-BTC-64000-170726' }) !== 'trend') {
  throw new Error('a call option was not routed to trend');
}
if (instrumentDisplaySlot({ symbol: 'P-BTC-64000-170726' }) !== 'trend') {
  throw new Error('a put option was not routed to trend');
}
if (instrumentDisplaySlot({ symbol: 'MV-BTC-65800-230726', entry_at_utc: '2026-07-23T01:50:00Z' }) !== 'morning') {
  throw new Error('a MOVE position was not routed by its actual IST entry time');
}
if (instrumentDisplaySlot({ symbol: 'MV-BTC-65800-230726', entry_at_utc: '2026-07-23T13:50:00Z' }) !== 'evening') {
  throw new Error('an evening MOVE position was not routed to evening');
}
if (instrumentDisplaySlot({ symbol: 'UNKNOWN-THING' }, 'evening') !== 'evening') {
  throw new Error('an unrecognised instrument ignored its fallback slot');
}
if (instrumentDisplaySlot({ symbol: 'UNKNOWN-THING' }, 'not-a-real-slot') !== 'evening') {
  throw new Error('an invalid fallback slot was not defended against');
}
"""
    result = subprocess.run([NODE, "-e", script], cwd=ROOT, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_entry_utc_clock_prefers_the_explicit_timestamp_over_the_plain_clock():
    script = r"""
const fs = require('fs');
const vm = require('vm');
vm.runInThisContext(fs.readFileSync('static/js/slots.js', 'utf8'));

if (entryUtcClock({ entry_at_utc: '2026-07-23T01:50:07Z' }) !== '01:50:07') {
  throw new Error('explicit entry_at_utc timestamp was not formatted as HH:MM:SS');
}
if (entryUtcClock({ entry_time_utc: '01:50:07' }) !== '01:50:07') {
  throw new Error('a plain clock string was not passed through');
}
if (entryUtcClock({}) !== '') {
  throw new Error('missing entry data did not fall back to an empty string');
}
"""
    result = subprocess.run([NODE, "-e", script], cwd=ROOT, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
