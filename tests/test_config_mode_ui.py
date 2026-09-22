import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def test_voice_config_has_a_direct_audio_test_action():
    source = (ROOT / "templates" / "config.html").read_text(encoding="utf-8")

    assert "Test Announcement" in source
    assert "function testVoiceAnnouncement()" in source
    assert "tradeVoiceAnnouncements?.test()" in source


def test_score_auto_selector_offers_explicit_live_mode_with_warning():
    source = (ROOT / "templates" / "config.html").read_text(encoding="utf-8")

    assert '<option value="live">LIVE — place real orders automatically</option>' in source
    assert "place real exchange orders without manual confirmation" in source
    assert "Every eligible trade uses ${Number(" in source
    assert "Blocked 5:30 PM–midnight IST" in source
    assert "not between 5:30 PM and midnight IST on a weekday" in source
    assert "It holds at most one bot-owned position" in source


def test_config_header_has_an_immediate_account_scoped_bot_toggle():
    source = (ROOT / "templates" / "config.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    for required in (
        'id="config-bot-toggle"',
        'role="switch"',
        "function configBotToggleChanged(checked)",
        "TREND_ENGINE_SCORE_AUTO_MODE: requestedMode",
        "checked ? (dryRun ? 'dry_run' : 'live') : 'disabled'",
        "Bot turned OFF — no new automatic trades will be opened",
    ):
        assert required in source
    for required in (
        ".score-config-hero-actions",
        ".config-bot-toggle",
        ".config-bot-toggle input:checked + .config-bot-toggle-track",
    ):
        assert required in styles


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_config_header_toggle_persists_bot_off_immediately():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/config.html', 'utf8');
const varsStart = source.indexOf('let configReady = false;');
const varsEnd = source.indexOf('const DEFAULTS =', varsStart);
const functionsStart = source.indexOf('function controllerSummary()');
const functionsEnd = source.indexOf('function modeSelectionChanged()', functionsStart);
if (varsStart < 0 || varsEnd <= varsStart ||
    functionsStart < 0 || functionsEnd <= functionsStart) {
  throw new Error('Bot toggle JavaScript was not found');
}

const elements = {
  'c-TREND_ENGINE_SCORE_AUTO_MODE': { value: 'live' },
  'c-DRY_RUN': { value: 'false' },
  'score-config-summary': { className: '', textContent: '' },
  'config-bot-toggle': {
    checked: true, disabled: false,
    setAttribute(key, value) { this[key] = String(value); },
  },
  'config-bot-toggle-label': { textContent: '' },
  'config-bot-toggle-detail': { textContent: '' },
};
global.document = { getElementById(id) { return elements[id] || null; } };
global.dryBanner = () => {};
global.confirm = () => true;
global.refreshTopbar = () => {};
global.toast = () => {};
let posted = null;
global.jpost = async (path, body) => {
  posted = { path, body };
  return { ok: true };
};

vm.runInThisContext(
  source.slice(varsStart, varsEnd) +
  source.slice(functionsStart, functionsEnd) +
  `configReady = true; loadedScoreAutoMode = 'live';`
);

async function verify() {
  await configBotToggleChanged(false);
  if (!posted || posted.path !== '/api/config' ||
      posted.body.TREND_ENGINE_SCORE_AUTO_MODE !== 'disabled') {
    throw new Error('Bot OFF was not persisted immediately');
  }
  if (elements['c-TREND_ENGINE_SCORE_AUTO_MODE'].value !== 'disabled' ||
      elements['config-bot-toggle'].checked !== false ||
      elements['config-bot-toggle-label'].textContent !== 'BOT OFF') {
    throw new Error('Bot OFF UI did not settle on the saved mode');
  }
}
verify().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_score_auto_mode_must_match_account_trading_mode():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/config.html', 'utf8');
const start = source.indexOf('function scoreAutoModeError(');
const end = source.indexOf('async function saveConfig()', start);
if (start < 0 || end <= start) {
  throw new Error('Score-auto mode validation helper was not found');
}
vm.runInThisContext(source.slice(start, end));

const cases = [
  ['disabled', 'true', ''],
  ['disabled', 'false', ''],
  ['dry_run', 'true', ''],
  ['dry_run', 'false', 'requires DRY RUN'],
  ['live', 'false', ''],
  ['live', 'true', 'requires LIVE'],
];
for (const [scoreMode, tradingMode, expected] of cases) {
  const actual = scoreAutoModeError(scoreMode, tradingMode);
  if (expected ? !actual.includes(expected) : actual !== '') {
    throw new Error(
      `${scoreMode}/${tradingMode}: expected ${expected || 'no error'}, got ${actual}`
    );
  }
}
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_mode_selector_locks_restores_and_ignores_stale_checks():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/config.html', 'utf8');
const varsStart = source.indexOf('let configReady = false;');
const varsEnd = source.indexOf('const DEFAULTS =');
const functionsStart = source.indexOf('function dryBanner()');
const functionsEnd = source.indexOf('function rememberSavedPreservedValues(', functionsStart);
if (varsStart < 0 || varsEnd <= varsStart ||
    functionsStart < 0 || functionsEnd <= functionsStart) {
  throw new Error('Trading Mode JavaScript was not found');
}

const attributes = {};
const modeSelect = {
  value: 'false',
  disabled: true,
  title: '',
  setAttribute(key, value) { attributes[key] = String(value); },
};
const modeHint = { textContent: '' };
const dryBannerElement = { style: {} };
const scoreConfigSummary = { className: '', textContent: '' };
global.document = {
  getElementById(id) {
    return {
      'c-DRY_RUN': modeSelect,
      'mode-lock-hint': modeHint,
      'dry-banner': dryBannerElement,
      'c-TREND_ENGINE_SCORE_AUTO_MODE': { value: 'disabled' },
      'score-config-summary': scoreConfigSummary,
    }[id];
  },
};

vm.runInThisContext(
  source.slice(varsStart, varsEnd) +
  source.slice(functionsStart, functionsEnd) +
  `
  global.setModeHarness = (ready, loaded) => {
    configReady = ready;
    loadedDryRun = loaded;
    modeDirty = false;
    modeChangeAllowed = false;
    modeAvailabilityBusy = false;
    lastModeAvailability = null;
  };
  `
);

const locked = {
  dry_run_mode: false,
  mode_change_allowed: false,
  mode_selection_enabled: false,
  verification_ok: true,
  mode_lock_reason: 'An open LIVE position exists.',
};
const allowed = {
  dry_run_mode: false,
  mode_change_allowed: true,
  mode_selection_enabled: true,
  verification_ok: true,
  mode_lock_reason: 'No open positions.',
};

setModeHarness(true, 'false');
modeSelect.value = 'true';
applyModeSelectionStatus(locked);
if (!modeSelect.disabled || modeSelect.value !== 'false') {
  throw new Error('locked status did not restore and disable the authoritative mode');
}
if (!modeHint.textContent.includes('open LIVE position') ||
    attributes['aria-disabled'] !== 'true') {
  throw new Error('locked status was not explained accessibly');
}

applyModeSelectionStatus(allowed);
if (modeSelect.disabled || attributes['aria-disabled'] !== 'false') {
  throw new Error('verified-flat status did not enable the selector');
}

modeSelect.value = 'true';
modeSelectionChanged();
applyModeSelectionStatus(allowed);
if (modeSelect.value !== 'true') {
  throw new Error('a fresh flat check erased the user’s unsaved mode choice');
}
applyModeSelectionStatus(locked);
if (!modeSelect.disabled || modeSelect.value !== 'false') {
  throw new Error('a newly opened position did not cancel the unsaved choice');
}

async function verifyRequestOrdering() {
  setModeHarness(true, 'false');
  const pending = [];
  global.jget = () => new Promise(resolve => pending.push(resolve));
  const older = refreshModeAvailability(true);
  const newer = refreshModeAvailability(true);
  if (pending.length !== 2) throw new Error('forced checks were not started');
  pending[1](allowed);
  await newer;
  pending[0](locked);
  await older;
  if (modeSelect.disabled || modeHint.textContent !== 'No open positions.') {
    throw new Error('an older position response overrode the newest verified state');
  }
}

verifyRequestOrdering().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
