# Security checklist

Against `Trend_Engine.md` §24. Items marked **ACCEPTED RISK** are knowing
decisions with rationale, not oversights.

## Credentials

- [x] **The engine holds no trading credentials.** v1 and the entire shadow
      window use public market data only ([ADR 0001](adr/0001-runtime-topology.md)).
- [~] **Phase 4: engine gets a separate read-only key** for the private feed.
      The trading key stays with `trend_score_live_execution.py`. *Code path
      built (`market_data/delta_private_ws.py`) and configured to read
      `ENGINE_READ_ONLY_API_KEY`/`_SECRET` by env-var name only — never a
      secret in TOML. Ships `enabled = false`: the auth-frame shape is
      unverified (assumption A10) and no read-only key has been issued yet.
      Both remain operator actions before this can be turned on.*
- [ ] **Confirm withdrawal permission is disabled** on every Delta API key in
      use. Operator action, not code.
- [x] No hard-coded keys. `dashboard.py` reads `API_KEY`/`API_SECRET` from
      `.env` or `users/<user>/account.json`.
- [ ] `.env.example` shipped; real `.env` stays gitignored (already is).
- [x] Paper and live are separated by `DRY_RUN` plus a distinct
      `users/<user>/dry_run/` namespace, with `test_dashboard_dry_run_isolation.py`
      enforcing it.
- [ ] **Phase 8: explicit production confirmation** before `TREND_SIGNAL_SOURCE=engine`
      can drive live orders (§24 "additional production confirmation setting").

## `users/<user>/account.json` stores API key and secret in plaintext
**ACCEPTED RISK**

The keys must be usable unattended by processes on the same host, so any
encryption key would have to live on that same host. Encryption buys very
little against the realistic threat (host compromise) and costs a one-way
migration across every account. Mitigations instead:

- [x] Gitignored.
- [ ] Tighten filesystem permissions: `icacls` to the single user on Windows;
      `chmod 0600` plus a dedicated service user on EC2.
- [ ] **Log-redaction test**: assert no API key or secret substring can appear
      in any log line. Extend `test_dashboard_account_credential_safety.py`.
- [x] `_mask()` already redacts keys in `/api/accounts` responses.

Revisit if the box ever becomes multi-user.

## Network exposure

- [x] Engine binds `127.0.0.1:5055` only. Never proxied, never public.
- [x] `X-Engine-Token` on every engine endpoint except `/health`. Not a trust
      boundary — it exists so a stray `curl` or misconfigured proxy cannot
      become an input to a trading decision.
- [ ] Verify port 5055 is free on EC2 and that nginx needs no change (A8).
- [x] Dashboard `_auth_gate` covers every path except
      `/login`, `/static/`, `/favicon.ico`, `/health`.

## Input validation

- [ ] Every WebSocket and REST payload validated before use; reuse the strict
      validators in `trend_engine_live.py:85-186`, which already encode Delta's
      response shapes.
- [ ] **Parse prices and sizes with `Decimal`, never `float`.** The REST book
      returns scientific notation (`"3.38E+3"`) and WS snapshot/update level
      encodings differ (A2).
- [ ] Reject any snapshot whose `timestamp` is in the future or older than TTL.
- [ ] Request signing exactly as documented; reuse `dashboard.py:_sign`.

## Operational

- [ ] Structured JSON logs with secrets redacted at the formatter, not the call
      site.
- [ ] `data/` gitignored alongside `users/` and `logs/`.
- [ ] Engine crash-loop stops after 10 restarts in 5 min so trading fails
      closed rather than flapping ([ADR 0001](adr/0001-runtime-topology.md)).
- [x] Kill switch tested before any live stage (criterion 11).
      `risk/kill_switch.py` latches across restarts and clears only via
      `POST /admin/resume`; covered by `tests/test_risk_kill_switch.py` and
      `tests/test_risk_api.py`. Still to be rehearsed end-to-end against a
      running engine before Stage C.
- [ ] Rollback is a config flip, never a code change.

## Pre-live gate

None of the Phase 8 live stages may begin until every unchecked box above that
is not marked ACCEPTED RISK is checked.
