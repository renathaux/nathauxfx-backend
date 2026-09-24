# Fast Backtest Performance Implementation Plan

Goal: replace browser-driven monthly FAST transport with owned asynchronous jobs, preserving exact trades and diagnostics.

Architecture: measure the unchanged 94d80e2 engine against canonical static files first. Optimize pure fact generation and trusted normalized evaluation under parity tests. Only introduce a backend-owned full-history job after proving that full-history initialization preserves legacy monthly warm-up/reset outputs. History is local or fetched from an allowlisted immutable source; no Neon history reads. Bound caches, job queues and worker memory. Replay retains its current short-window endpoint.

Spec: /Users/nathauxfx/.codex/attachments/e5fc6511-e0e6-4af1-9a31-544aed1f6794/Pasted text.txt

Constraints: unchanged evaluator/trading semantics; zero unexplained trade/metric/diagnostic differences; no production deployment before parity passes; backend before frontend. Actual Gold definitions must be obtained after account authentication, never inferred from names/screenshots.

- [x] Profile and freeze: benchmarks/profile_fast.py instruments static JSON loading/parsing, Pydantic parsing, bundle aggregation, structure/swings/trend/EMA facts, evaluator, resolution, diagnostics and serialization. Store outputs outside repository; compare exact dictionaries/trades with reference source from 94d80e2.
- [x] Establish full-range versus old 31-day boundary parity before selecting job preprocessing design. Explicitly test active/pending/remembered setups and warm-up/EMA initialization effects.
- [x] Optimize confirmed-swing availability with precomputed prefix direction; cache trend keys; avoid repeated normalization using internal trusted evaluator entry. Test original helpers against optimized facts at every timestamp and prefix truncations.
- [x] Profile again; optimize remaining measured bottlenecks with parity tests and bounded memory measurements.
- [x] Add bounded backend history/fact cache keyed by immutable dataset digest and timeframes; test invalidation and ownership-safe jobs. New fast-jobs create/status/cancel endpoints never touch broker orders or LIVE state.
- [x] Wire FAST frontend create/poll/cancel; retain Replay and exact Studio autostart inputs. Remove old production FAST chunk transport only after passing parity.
- [ ] Run Gold 831/832 and EURUSD trade/metric/diagnostic comparisons; report measured runtime and peak memory; review changes; deploy backend then frontend only if parity and memory gates pass.

Review focus: initialization resets across legacy monthly boundaries; HTF availability timing; dataset updates during a job; unauthenticated/cross-owner status reads; cancellation/failure preserving previous completed results.

Measured full-history initialization does not preserve old trades. The implementation retains legacy EMA/BOS warm-up resets internally, while history/aggregation and swing detection are shared once. See docs/performance/fast-backtest.md. Deployment remains blocked pending authenticated saved Gold definitions and Render resource validation.
