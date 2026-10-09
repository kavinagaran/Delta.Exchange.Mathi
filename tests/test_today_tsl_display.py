import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_paper_policy_renders_dynamic_peak_giveback_and_floor_without_flag():
    script = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync('templates/overview.html', 'utf8');
global.esc = String;
global.fN = (n, dp=2) => Number(n).toFixed(dp);
global.f$ = n => '$' + Number(n).toFixed(2);
vm.runInThisContext(source.slice(source.indexOf('function todayKnownNumber('),
  source.indexOf('function renderTodayCurrentTrade(')));
const protection = {running: true, protection_mode: 'filled_premium_percent_peak_trail_v2',
  tsl_armed: true, tsl_arm_pnl: 0, peak_pnl_usd: 44.21, tsl_floor_usd: -274.79};
let html = todayInlineProtectionHtml(protection);
for (const expected of ['Peak P&amp;L $', 'TSL giveback $', 'TSL floor $', '$44.21', '$319.00', '$-274.79', 'TSL ARMED']) {
  if (!html.includes(expected)) throw new Error('Missing: ' + expected);
}
html = todayInlineProtectionHtml({...protection, peak_pnl_usd:400, tsl_floor_usd:200});
if (!html.includes('$400.00') || !html.includes('$200.00')) throw new Error('Telemetry did not refresh');
if (html.includes('Minimum locked $') || html.includes('TSL arm P&amp;L $')) throw new Error('Legacy fields remain');
"""
    result = subprocess.run([shutil.which("node"), "-e", script],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
