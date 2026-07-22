import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def test_trend_engine_explains_the_daily_btc_expiry_policy():
    template = (ROOT / "templates" / "trend_engine.html").read_text(
        encoding="utf-8"
    )

    assert "Daily BTC expiry rule:" in template
    assert "at least 1h 30m remains" in template
    assert "less than 1 hour 30 minutes remaining are excluded" in template
    assert "configured settlement buffer" in template
    assert "Every approved plan uses a configured safety buffer" in template
    assert "['expiry_pass', 'Time to expiry']" in template
    assert "expiry: 'Expiry eligibility'" in template
    assert "contract.time_to_expiry_hours" in template
    assert "contract.days_to_expiry) * 24" in template
    assert " DTE`" not in template
    assert "The expiry is outside the permitted range" not in template


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
def test_time_to_expiry_is_rendered_without_overstating_minutes():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/trend_engine.html', 'utf8');
const start = source.indexOf('const displayExpiryTimestamp');
const end = source.indexOf('const joinValues', start);
if (start < 0 || end <= start) throw new Error('expiry display helpers not found');

global.hasValue = value => value !== null && value !== undefined && value !== '';
global.finite = value => hasValue(value) && Number.isFinite(Number(value));
vm.runInThisContext(source.slice(start, end) + `
  const exact = contractTimeToExpiryHours(
    {expiry: '2026-07-22T12:00:00Z'},
    {timestamp: '2026-07-22T10:30:00Z'}
  );
  if (exact !== 1.5 || formatTimeToExpiry(exact) !== '1h 30m remaining') {
    throw new Error('exact 1.5-hour boundary was not shown accurately');
  }
  if (formatTimeToExpiry(1.499) !== '1h 29m remaining') {
    throw new Error('remaining time was rounded up across the safety boundary');
  }
  if (formatTimeToExpiry(0.75) !== '45m remaining') {
    throw new Error('sub-hour expiry was not shown in minutes');
  }
  if (formatTimeToExpiry(-0.1) !== 'Expired') {
    throw new Error('expired contract was not identified');
  }
  if (formatTimeToExpiry(0.001) !== 'Less than 1m remaining') {
    throw new Error('sub-minute expiry was not identified');
  }
  const canonical = contractTimeToExpiryHours(
    {time_to_expiry_hours: 3.25}, {}
  );
  if (canonical !== 3.25) throw new Error('canonical TTE field was ignored');
  const legacy = contractTimeToExpiryHours({days_to_expiry: 0.125}, {});
  if (legacy !== 3) throw new Error('days-to-expiry fallback was ignored');
  const label = contractExpiryLabel(
    {expiry: '2026-07-22T12:00:00Z'},
    {timestamp: '2026-07-22T10:30:00Z'}
  );
  if (!label.includes('1h 30m remaining') || label.includes('DTE')) {
    throw new Error('contract label is not using clear time-to-expiry copy');
  }
`);
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
