# Immutable stream generations implementation plan

> Execute locally with test-driven development. No production connection, push, deploy, migration, orders or LIVE setting changes.

Goal: preserve legacy execution records exactly while bootstrapping and atomically selecting a non-executing authoritative replacement.

Architecture: normalized generation registry and one head per original storage key/timeframe; separate generation storage namespaces reuse immutable candle/event tables without changing any historical identifier. Generation 1 retains original keys and contents. All execution claims and writers fence against the same head. Pure administrative module imports no services package or broker adapter; consumes an explicitly scoped authoritative closed-history export.

Spec: user implementation request dated 2026-09-25 and outputs/xau-forensics-safe-recovery-design.md in task workspace.

Dependency map:
- models.py / migrations: legacy stream keys, candle uniqueness, event PK, lifecycle/attempt link by immutable event ID. Add registry/head metadata only.
- indicator_event_stream_service.py: merge, origin, identity, event read, lifecycle. Fence writes and eligibility; explicit audit read separate.
- indicator_stream_account_scope.py: installed startup/LIVE/chart wrappers resolve current generation per timeframe. No cached head.
- services/__init__.py: startup provider/origin wrappers receive resolved storage namespace; no admin import (package installs trading wrappers).
- app_bootstrap.py startup gate -> installed scoped initialize -> active namespace.
- smc_strategy_authority.py / paper_v3b_bridge.py -> scoped authority and durable event read. Confirmation/setup/submission IDs transitively include event ID.
- trade_submission_service.py: atomic claim checks generation while holding head lock; pending attempts prevent cutover. Old lifecycle transitions fenced.
- indicator_candle_display_reader.py / strategy_simulator_data_source.py / strategy_lab/data_source.py: deliberate namespace resolution.
- Strategy Studio live candidate: independent evaluator/lifecycle; must capture generation and cutoff, discard stale evaluator state, namespace setup IDs, validate again at claim.
- old recovery CLI/service: reject inactive legacy storage namespace; never rebuild frozen audit rows.
- new admin CLI: explicit dry-run/apply with approved plan, snapshot, closed history, LIVE OFF and one final DB transaction.

Tasks:
- [x] Add failing incident/namespace/preservation tests; add registry schema and migration assigning existing streams generation 1 without updating them.
- [x] Implement pure plan/apply, strict history validation, retained full prefix replay, snapshots, atomic pointer switch, idempotency and audit reads.
- [x] Integrate current readers/writers and execution/lifecycle fences. Add startup/cutover/race tests.
- [x] Integrate independent Strategy Studio evaluator and claim fences; namespace continuation and setup identity.
- [x] Add broker-free administrative CLI with plan/snapshot inputs, default dry-run, apply guards and operator documentation.
- [x] Run required local incident cases, replay equivalence, schema migration, regressions and independent branch review. Record limitations honestly.

Review focus: cross-generation claims during cutover; cold restart and missing registry; confirmation watermark equality; stale Studio evaluator state; malformed/partial/duplicate or forming history.

Ruling: operator supplies cTrader history export to the pure migration CLI; a broker adapter must not be imported by the migration process. This separates data acquisition from non-executing recovery capability.

Verification: 213 selected tests passed locally. Required generation cases, additive migration, concurrent apply/claim, restart/continuation, Studio fences, CLI defaults and import isolation covered. PostgreSQL runtime locking was reviewed, not exercised on a PostgreSQL server. No production connection or mutation.
