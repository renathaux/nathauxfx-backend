# Relinquishment Concurrent Observation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement task-by-task.

**Goal:** Remove unnecessary serial read latency without changing freshness or safety decisions.

**Architecture:** Administrative-only reader overlaps independent complete DB/broker reads after fresh credentials. Caller-thread broker, one joined DB worker, no ownership lock held. Existing runtime adapter and20s deadline unchanged.

**Tech Stack:** Python, concurrent.futures, SQLAlchemy, existing read-only broker reader, disposable PostgreSQL.

**Spec:** `docs/superpowers/specs/2026-10-15-relinquishment-concurrent-observation-design.md`

## Global Constraints

- Preserve both existing optimizations, full history/lineage/reconciliation/fencing and20s deadline.
- No production writes until independent review, certification/publication and fresh transition preflight.
- LIVE OFF; no orders; Gold authority remains Strategy Studio through real pipeline.

## Review Focus

- Thread ContextVars must not hide an active ownership transaction.
- Exception/timeout must join worker and never yield partial evidence.
- Account switch during overlap must block downstream mutation.
- Independent calls must never share observations or credentials.
- Timing evidence must include real locking and distinguish floors from payload measurements.

### Task1: Certified concurrent reader

Files: create `Backend/startup_recovery/handoff_reader.py`, `Backend/tests/test_handoff_reader.py`.
Interface: `ConcurrentHandoffReader(engine, *, history_from)`; callable(scope) returns complete database/broker mapping; database(session,scope) performs existing fresh read_connection.

- [x] Add barrier-based overlap, caller-thread broker, joins/errors, transaction/single-flight, expiry and repeated-read tests; verify RED.
- [x] Implement only reader scheduling with existing reader dependencies; verify GREEN.
- [x] Verify complete snapshot equivalence and adversarial adapter tests in disposable PG.

### Task2: Representative full-path validation

- [x] Wire certified concurrent reader into synthetic protocol harness; real DB and locks, six streams37lineage visits, all observed production cost components preserved without double counting.
- [x] Run bounded repeated observed and sensitivity profiles; assess final freshness timing and lock variance, not arbitrary threshold.
- [x] Review whole change and resolve blockers; local owner/reader/lock/fencing suites 318/318. Full native release suites remain Task3.

### Task3: Certification and controlled transition

- [ ] Commit/freeze using existing export mechanism; record new identities.
- [ ] Native AMD64 full suites and independent reproducible image builds; verify downloaded OCI and publish exact bytes.
- [ ] Fresh production authority/broker preflight; one certified relinquishment only; one successor deployment; runtime identity, recovery and hosted PREENABLE WOULD_ALLOW; LIVE OFF.

## Execution ruling

Native execution authorized without routine plan approval. Independent design analysis endorses scheduling concurrency over unproven immutable-generation reuse. Plan progress and evidence are retained locally; do not infer later gates from earlier tests.

## Reviewed performance ruling, 2026-10-15

Actual maintenance-isolated read-only concurrent observations: 16.188989,
15.058259, 15.631485 seconds. Production row locks were not acquired.
Disposable full-path PostgreSQL trials used six streams, 37 lineage visits,
two complete broker observations, and 3,432,850-byte synthetic snapshots
(production measured 3,403,219 bytes). No measured DB/filesystem/broker writes.
Six recalibrated final checks: 16.765463, 15.780942, 16.478114, 16.951345,
16.972426, 16.938001 seconds. Worst pre-denied-write total: 17.119003 seconds.
The unchanged 20-second contract retains 3.027574 seconds at the worst final
check. Observed path variation plus three extra owner/account query round trips
at the observed non-payload SQL maximum gives a conservative 1.421998-second
reserve, leaving 1.605577 seconds. NOWAIT lock contention remains fail-closed.
Independent whole-change and timing review: no blocking findings. This is
evidence for certification and one fresh-preflight maintenance-held attempt,
not a statistical latency guarantee. No production handoff has been executed.
