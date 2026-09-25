# Controlled deployment readiness — 2026-09-25

Do not deploy until the owner approves. Keep Render at 512 MiB, one instance.

## Shared admission audit

All production HTTP replay entry points share `heavy_replay_lease`, a nonblocking
OS flock on `/tmp/nathauxfx-heavy-replay.lock` (server configuration may override
the path, but all processes must use the same path). Maximum admitted heavy work
is one per host. This is not a distributed lock for multiple Render instances.

- FAST `/strategy-simulator/fast-jobs`: bounded queue; supervisor obtains lease
  before spawning; child inherits descriptor; supervisor releases after reaping.
- Studio Run Backtest redirects to Simulator and uses the same FAST endpoint.
- Legacy `/strategy-simulator/run` and `/strategy-lab/replay`: middleware holds
  lease through response serialization. Busy requests receive HTTP 429.
- `/strategy-studio/parity/run`: now included in the same middleware.
- Studio `/live-status` and enable `/live-handoff` also calculate seven-day
  historical parity. The readiness helper now acquires the same lease; busy
  readiness is unavailable, and an enable request returns 429 without mutation.
  Disabling handoff does not acquire the permit. Broker execution is unchanged.
- Manual Replay reads static history in the browser; retired `/manual-history`
  remains guarded and returns 410 when admitted. It starts no backend worker.
- No further production HTTP replay aliases/callers were found. Offline benchmark
  entry points are operator tools, not production routes; do not run those beside
  production workload because they do not obtain the API admission permit.

Real-child tests cover independent FAST managers, both FAST/legacy orderings,
Studio/FAST, cancellation, exception, timeout, crash, and permit release after
reaping. Child numerical payloads are substituted in these tests; existing
five-year exact-parity and Linux memory evidence remains separate.

## Existing collection issue

`tests/test_multi_user_auth.py:6` imports deleted
`services.deriv_user_connection_store`. Commit 6b12c2d removed Deriv on September 3;
it precedes c21b724, 10f1426 and 5d78a20. The obsolete test blob is identical across
those versions. Production does not import this module, and Render startup does
not run pytest. Classification: UNRELATED EXISTING TEST ISSUE.

The expanded Studio tests additionally require the SQLite schema bootstrap in
`.github/workflows/strategy-studio-stage3.yml`. Without it, the account-switch test
fails on missing `strategy_setup_lifecycle`, including on unchanged 5d78a20.
With that existing setup it passes. Production already has the table migration;
this task neither alters nor applies migrations to Neon.

## Unexecuted deployment plan

A. After explicit approval, fetch and compare remote main with the verified branch;
   stop on divergence and revalidate any integration. Push the verified commits to
   the configured deployment branch without force. Record previous live SHA.
B. Allow normal Render auto-deploy. Do not change plan, instance count, credentials,
   Neon, saved definitions, or LIVE Auto. Verify FAST feature flag/configuration;
   if disabled, obtain authorization before changing it. No migration changes are
   included; existing startup retains its normal Alembic invocation.
C. Verify deployed commit equals the final approved SHA, and deployment is live.
D. Check health and normal existing LIVE/data processing; record restart counters.
E. Observe stable idle memory before starting. Start continuous instance memory,
   stage, worker PID and restart/OOM observation first. Confirm no replay active.
F. Run exactly ONE authenticated five-year FAST job: frozen Gold 831 definition,
   XAUUSD, 2021-09-25T00:00:00Z through 2026-09-24T00:00:00Z, balance 10000,
   pinned history b09874420ce975ee83583d3ba35dbaaf36a5f1a3. Do not save/edit the
   strategy. Check the snapshot/history match before launching; no concurrent or
   additional backtest is permitted in this verification.
G. Record actual Render total-instance memory from before download through
   processing, serialization, child exit and cleanup. Record each phase peak,
   overall peak, post-job memory, OOM/restart events and observation resolution.
   Parent RSS or child RSS alone is insufficient; missing telemetry is inconclusive.
H. Compare trades, metrics, diagnostics and equity curve exactly with frozen
   reference (486 trades; canonical parity SHA256
   49b57da42edc58998c9398e2be1eac1c2882ed98b34f7ef49f5ae166fd554735).
   Exclude volatile performance timing from equality. Use the same canonical
   serialization as the reference; also compare complete sections directly.
I. Confirm worker exit/reaping, released admission, no zombie and no partial scratch.
   Do not launch a second job to test release in production.
J. Observe memory returning toward the pre-job baseline and stabilizing. Retained
   completed-result disk files are expected; retained worker RSS is not.
K. Confirm existing LIVE/data health and restart counters remained normal using
   read-only status/logs. Do not enable LIVE Auto or place a test broker order.

Desired actual peak <=420 MiB. A 420–440 MiB result misses the desired target and
requires review; do not infer approval for another test. Above 440 MiB STOP further
tests and report. Approaching 480 MiB is unsafe: cancel the active job if possible,
observe reaping, and run no further backtest. At any OOM, restart, parity mismatch,
failed cleanup or material LIVE/data degradation, stop and report. Recommend
rollback to the recorded prior SHA if unsafe; do not execute unapproved rollback.
The 406.17 MiB projection is local Linux evidence, not an actual production peak.
