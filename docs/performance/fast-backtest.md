# FAST backtest jobs

Status: implemented locally; **not approved for production rollout yet**. The API is disabled unless `SIMULATOR_FAST_JOBS_ENABLED=1`. Saved Gold 831/832 parity, actual Render memory capacity, and authenticated production verification remain rollout gates.

## Execution and data

FAST creates one owned job, polls status, and renders its completed result. Replay keeps the existing short-window endpoint. Jobs run serially in a separate Python process; one active job per owner, two globally queued/running jobs, ten-minute worker timeout, twenty retained jobs, one-hour cleanup age. Cancellation never publishes a partial completed result. Restarted unfinished jobs fail explicitly. This manager requires the existing single-process, single-instance service topology; do not horizontally scale it without replacing the process-local queue/ownership lock with shared coordination.

The worker loads canonical M5 JSON once and aggregates 15m/1h/4h once. Prefer `SIMULATOR_HISTORY_DIR` pointing to a mounted canonical dataset. Otherwise, the allowlisted GitHub source is pinned by `SIMULATOR_HISTORY_REVISION` (full commit SHA, default frontend b09874420ce975ee83583d3ba35dbaaf36a5f1a3). A dataset release must update that pin deliberately. Requests cannot supply arbitrary sources. No Neon candle reads or broker order mutations occur.

Remote files use a private 128 MiB disk LRU (`SIMULATOR_HISTORY_CACHE_DIR`, default `/tmp/nathauxfx-fast-history-cache`) plus a 64 MiB process LRU. Mounted files are reread and hashed each job. Immutable facts use a separate 128 MiB disk LRU under the job directory, keyed by dataset content, bounds, timeframes, and source-code version. Only facts are cached; strategy decisions, account balances, and trade states are not reused.

## Why preprocessing retains legacy initialization windows

The old 31-day HTTP transport unintentionally defined trading semantics: every request reset EMA seeds and the BOS engine to that window's warm-up boundary, and opposite-swing targets could only see swings inside that window. Removing those resets changes real trades. Examples from canonical XAUUSD: one-year 4h EMA200 produced 170 old trades versus 168 uninterrupted-history trades; five-year opposite-swing targets produced 2,813 versus 3,003.

Exact parity takes priority. The new worker retains those initialization boundaries internally. It detects confirmed swings once per required timeframe and shares candle arrays across views. Stateful BOS and EMA initialization remain per legacy window, intentionally departing from the originally requested global-only fact construction. They account for a small fraction of runtime after removing repeated history upload/aggregation, full-array swing scans, iterrows overhead, and per-candle definition normalization. Sequential continuation preserves active trades, pending setups, remembered BOS, and compounded balance. No timeline segments run concurrently.

## Verification and measurements

Use a frozen copy of backend 94d80e2 to establish baselines; never regenerate expected results using optimized code:

```sh
PYTHONPATH=/path/to/reference/Backend python Backend/benchmarks/profile_fast.py \
  --history /path/to/replay-data --definition /path/to/definition.json \
  --start 2021-09-25 --end 2026-09-24 --symbol XAUUSD --output /path/to/frozen-output

PYTHONPATH=Backend python Backend/benchmarks/verify_fast.py \
  --baseline /path/to/frozen-output --history /path/to/replay-data \
  --cache-dir /path/to/facts-cache --output /path/to/comparison.json
```

`--sections` on the baseline profiler adds in-memory timers to frozen EMA/trend/diagnostic sections, without changing trading expressions or source files. Normal profiling wraps parsing, aggregation, structure, confirmed swings, evaluator calls, resolution and serialization. Timers are nested and must not be summed as independent costs. The frontend performance harness stubs backend evaluation, so its request timing is not real WAN/server latency.

Local five-year synthetic comparisons (not the saved Gold 831/832 strategies): representative XAUUSD 57.30s → 10.77s, EURUSD 65.46s → 11.30s, XAUUSD with 0.40–0.60% SL filter 68.78s → 11.31s. Each compares every complete trade dictionary, all metrics, equity points and diagnostics exactly. Worker peak RSS was about 300–303 MB on those tests and 388 MB for the five-year opposite-swing test. Numeric history occupies about 17–18 MB. Representative completed result JSON is about 697 KB. A fresh process with cached facts completed in 5.60s at 195 MB peak RSS. These are local mounted-history timings, not Render timings; the reported production multi-hour run has not yet been reproduced with authenticated saved strategies.

Local network measurement of the pinned remote source: cold history load alone 28.99s (62 downloads, 40.52 MB), versus 2.38s in a fresh process using disk cache (zero downloads). These are not full remote-backed backtest timings. A mounted dataset avoids first-job WAN latency. Include the web process and raw-file cache when budgeting Render RAM; subprocess isolation alone does not protect against a shared cgroup OOM.

## Rollout gates

1. Authenticate and read the real saved Gold 831 and Gold 832 definitions. Compare frozen/optimized complete results, including the specified Gold 832 SL filter. Synthetic fixtures are not substitutes.
2. Confirm the selected Render instance's memory and process topology. Measure total application plus worker memory before enabling jobs.
3. Deploy backend first with the required immutable history configuration; enable only after parity/resource checks pass. Then deploy frontend. Do not enable the new frontend against a disabled endpoint.
4. Verify deployed commits and run an authenticated read-only FAST test. Do not activate LIVE, place orders, change positions, saved definitions or production candles.
