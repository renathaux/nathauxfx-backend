# DEMO Broker Integration Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Prove one tracked DEMO broker round trip without touching V3B eligibility or LIVE Auto preferences.

**Architecture:** Dedicated durable test submissions, persistent account fence, pinned broker adapter, explicit CLI. Normal execution participates in account coordination; unresolved test state fails closed after restart.

**Tech Stack:** Python, SQLAlchemy, Alembic, PostgreSQL, cTrader JSON protocol, pytest.

**Spec:** docs/superpowers/specs/2026-09-16-demo-broker-test-design.md

## Global Constraints

- Only account `47784297` freshly verified DEMO; `47810571` forbidden.
- LIVE Auto preference stays ON; account selection unchanged.
- No strategy/candle/event/lifecycle fabrication or mutation by the test.
- Exactly one opening send per test ID; ambiguous dispatch is reconciliation-only.
- No production access, deployment or broker calls by implementer/reviewer agents; controller alone handles release and authorized proof.
- Preserve existing dirty artifacts. Commit only task source/tests/docs; use PYTHONDONTWRITEBYTECODE=1 and isolated SQLite for local tests.

## Task 1: Implement and verify the isolated durable round-trip subsystem

**Files:**
- Modify Backend/models.py for dedicated submission model.
- Create Backend/migrations/versions/20260916_0022_broker_integration_test.py.
- Create Backend/services/broker_integration_test_service.py for durable state machine.
- Create Backend/services/broker_integration_test_adapter.py for pinned broker preflight/send/read/close.
- Create Backend/services/account_execution_coordination.py for shared account locking/fencing.
- Modify Backend/api.py narrowly at normal execution wrapper (no strategy/risk guard changes).
- Create Backend/scripts/run_broker_integration_test.py (no public route).
- Create Backend/tests/test_broker_integration_test.py and focused adapter/coordination tests as needed.
- Modify .github/workflows safety workflow to include new tests if explicit test selection is used.
- Create Backend/docs/broker_integration_test.md documenting invocation/recovery and exact fail-closed semantics.

**Interfaces:**
- CLI requires --account-id, --test-id, --symbol EURUSD, --confirm-demo-broker-test; default full round trip. --preflight performs reads only; --recover resumes same ID without opening a new order.
- Service uses injected broker adapter/session factory for unit tests; production adapter has fresh_preflight, submit, reconcile, close methods. Finalize explicit signatures with typed request/evidence objects in service module.
- Coordination is shared by test service and normal order execution, only special-cases the permitted test account, and preserves ordinary behavior for other accounts. Use nonblocking acquisition/fail-closed behavior to avoid hanging dashboard refresh.

- [ ] Step 1: Read existing connector and submission paths, schema/migration conventions and focused CI. Write tests exercising real service against isolated DB, with broker network boundary fake that counts sends and asserts persisted identity exists before send. Before implementation run `python -m pytest -q tests/test_broker_integration_test.py` and capture RED.

Required behavior cases:
```python
# Real service + isolated SQLAlchemy DB; only broker network boundary is faked.
# Wrong/live/missing preflight -> sends == 0, strategy rows unchanged.
# Broker submit callback queries a committed request_started_at and stable ID.
# service.run(same_request) twice -> sends == 1, terminal CLOSED/RECONCILED.
# Network exception after request marker -> recover same ID -> sends == 1.
# Cleanup unavailable -> durable fence survives service/session reconstruction.
# Normal strategy callback never runs while test fence exists; runs after closure.
# Unrelated account callback is unchanged.
```

- [ ] Step 2: Implement the model/migration, durable state machine and coordination. Commit before-send evidence separately from network work; do not keep an uncommitted marker as the only evidence. Use explicit transitions, immutable test identity, unique unresolved-account constraint, and atomic lock ordering. On errors return durable recoverable state; never infer closure from timeout or incomplete data.
- [ ] Step 3: Implement pinned broker preflight and close evidence. Consult official cTrader protocol docs for units and field semantics. Do not guess volume scaling or environment booleans. No fake SL/TP strategy plan; any broker protection is labeled test-only and documented. Persist sanitized evidence, not tokens or full credentials.
- [ ] Step 4: Implement CLI and recovery usage, then run new tests plus existing atomic claim, submission taxonomy/reconciliation, V3B handoff tests and CI safety suite. Verify existing normal behavior preserved.
- [ ] Step 5: Self-review for account switches, cross-worker race, crash windows, partial broker evidence, duplicate opening calls and close volume mismatch. Commit scoped changes. Write report with exact RED/GREEN commands/results, files/commits and any remaining concerns. No pushing/deploying/production calls.

## Controller release checklist

- [ ] Independent task and final branch review; resolve important findings.
- [ ] Push isolated branch, create PR, wait green CI, merge expected head only.
- [ ] Verify Render auto-deploy exact merge SHA LIVE.
- [ ] CLI preflight confirms broker DEMO account, volume and cleanup readiness; record current LIVE Auto ON and both READY.
- [ ] Run exactly one new ID; verify duplicate call reconciles without new send; capture open/close broker IDs and durable state.
- [ ] Confirm CLOSED/RECONCILED, no open test position, coordination released, account unchanged, LIVE Auto ON and both streams READY.
