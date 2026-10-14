# Runtime Identity Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose authenticated observations of the actual application process without creating or implying trading authority.

**Architecture:** A process-local recorder captures allowlisted facts at existing startup boundaries. A dedicated GET route authenticates through existing SELECT-only predicates and returns detached snapshots. Neither observations nor recording failures may alter recovery decisions, transactions, publication, or dispatch.

**Tech Stack:** Existing Python, FastAPI, SQLAlchemy/PostgreSQL, pytest and native AMD64 certification harness; no new dependencies.

**Spec:** User approval dated 2026-10-14, attachment `f9ffb08d-4786-4906-99ef-c13c76d49689/Pasted text.txt`. The approval is authoritative; this plan does not authorize production operations.

## Global Constraints

- Starting product HEAD: `4c1c483086f08a8c2596cb400725071d03a1249e`; reuse existing isolated worktree.
- No production access, deployment, restart, maintenance switch, ownership relinquishment, broker access/writes, hosted production verifier, or LIVE enablement.
- No changes to Gold, Studio, risk/protection, execution, recovery/fencing/adoption semantics, dependency locks or base-image contracts.
- No inference of authorization from startup success, recorded ownership, installed token or producer registration. Broker mutation authority remains `UNKNOWN` without an existing safe explicit current proof.
- GET `/admin/runtime-identity` is independent of the hosted-verifier flag; every response is `Cache-Control: no-store`.
- Unknown, unobserved, stale and ambiguous facts remain null/`UNKNOWN`; no default-false substitution.
- Historical BLS backfill remains outside this release's supported surface. Preserve reviewed test-only auth overlay and broad diagnostic comparability rules.
- Full certification, not reduced certification; OCI retention only, no publication.

## Review Focus

1. A commit failure must not publish allocated epoch or acquired ownership as committed.
2. Auth failures must neither expose exception/credential contents nor refresh or clean sessions.
3. Concurrent reads must return detached internally consistent recorder snapshots; later startup updates cannot mutate an earlier response.
4. Durable owner observations and startup facts must not masquerade as current process authority.
5. Observation failures must not change recovery calls, exception handling, transaction count, order or outcomes.

## Task 1: Recorder and startup boundary observations

**Files:** Create `Backend/startup_recovery/observability.py` and `Backend/tests/test_runtime_identity_observations.py`; narrowly modify `bootstrap.py`, `coordinator.py`, `store.py` and `server_adapter.py` in `Backend/startup_recovery/`.

**Interfaces:** `StartupRecorder.snapshot()` returns a detached JSON-safe projection; an observation context associates the recorder with the existing startup invocation. Event recording accepts only defined fields, never arbitrary exceptions, mappings or credentials. Transaction-local observations are staged and promoted only after the existing transaction exits successfully.

- [ ] Write failing tests for committed attempt UUID/epoch/creation time, busy rejection, pre-acquisition failure, BOOTSTRAP failure, commit ambiguity, observation failure isolation, immutable copies and concurrent reads. Run and record genuine RED before implementation.
- [ ] Implement lock-protected recorder, process identity and explicit unknown defaults. PID is the serving process PID. Use OS process-start evidence where available; otherwise null, never substitute module-import time. Render identity is an allowlisted bounded identifier, not an environment dump. Build fields are explicitly allowlisted.
- [ ] Observe actual attempt creation timestamp where it is constructed in `store.begin_attempt`, and existing account-row owner data where already read. Do not add SQL just to obtain observations. Keep the existing token return value and transaction boundaries. Stage attempt data until commit succeeds; failed/ambiguous commit leaves committed fields unknown.
- [ ] Record acquisition attempted immediately before its existing invocation; committed success only after transaction exit. Record actual rejection codes, not arbitrary exception text. Owner data read before rejection is timestamped historical observation, not a successful acquisition.
- [ ] Observe token installation and successful producer registration at their actual separate boundaries in `publish_reconciled`. Record submission reconciliation's allowlisted outcome, not payloads. Freeze startup facts when bootstrap completes, including failure paths.
- [ ] Run recorder and existing bootstrap/coordinator/recovery tests on disposable PostgreSQL. Compare call/transaction behavior with recording unavailable versus enabled. No failures/errors/skips in required cases.

## Task 2: Authenticated serving-process endpoint

**Files:** Create `Backend/routes/runtime_identity.py` and `Backend/tests/test_runtime_identity_endpoint.py`; narrowly modify `Backend/startup_recovery/asgi.py` for installation.

**Interfaces:** `install(api)` registers the route and binds the actual API process recorder. The route returns `schema_version`, `process`, `startup`, `runtime_observation`, `observed_durable_owner`, `authority_observation`, `captured_at`.

- [ ] Write failing HTTP tests: anonymous/expired/non-admin denied, valid modern session requires CSRF, valid legacy-owner accepted through existing predicate, malformed/duplicate authorization rejected, verifier flag irrelevant, no-store on success and failures.
- [ ] Implement using `live_integrity.auth.read_admin` / `read_legacy_owner` inside `live_integrity.read_store.read_only`. Do not invoke verifier workers, normal session refresh APIs, broker readers or account synchronization. No new auth semantics or fallback.
- [ ] Runtime fields are strict local observations with their own timestamp: LIVE boolean or unknown, token presence, producer state. If execution-active or current producer authority cannot be safely proven from existing local state, report unknown rather than perform fencing/DB/broker work. Diagnostic dispatch availability is always false; broker mutation authority is `UNKNOWN`.
- [ ] Ensure the diagnostic route does not accidentally invoke recovery middleware's normal runtime capability evaluation. A diagnostic-only bypass must leave all other routes unchanged and provide no capability to this route; test exact method/path isolation and auth enforcement. Stop for review if isolation requires changing general request authority semantics.
- [ ] Test actual endpoint behavior with PostgreSQL-enforced read-only auth and fail-fast forbidden-operation sentinels: no epoch allocation, recovery, broker connection, credential refresh, account sync, publication, LIVE mutation or filesystem writes. Repeated calls preserve application state. Include token absent, producer false, startup success with broker authority still unknown, owner/process identity mismatch and concurrent snapshots.
- [ ] Run both new suites and all affected auth bridge, hosted PRE-ENABLE, recovery, architecture and owner suites. Preserve original assertions and required counts; add new tests rather than lowering gates.

## Task 3: Documentation, Option-B plan and independent review

**Files:** Create `docs/runtime-identity.md` and `docs/superpowers/plans/2026-10-14-observable-option-b-transition.md`.

- [ ] Document the exact response schema, timestamp scopes, unknown rules, credential exclusions, immutable historical startup versus current local observations, and absence of dispatch authority.
- [ ] Write PLAN ONLY: fresh safety preflight; isolate in approved maintenance without relinquishment; positively prove all predecessor processes terminated (traffic removal is insufficient); verify maintenance has no automatic recovery/migration/dispatch/state repair; recheck durable authority; separately reviewed epoch-6 relinquishment; exactly one successor startup using the allocator's next epoch, never forcing epoch 7; identity/checkpoint/broker evidence; separately authorized hosted PRE-ENABLE; stop with LIVE OFF.
- [ ] Define stop/rollback points: before termination proof preserve owner; ambiguous relinquishment inspect only/no blind retry; failed successor leave fail-closed and do not restore a prior token, fabricate SQL authority or start repeated successors. Any maintenance fallback/redeploy needs its own authority and fresh safety review.
- [ ] Obtain fresh independent review of implementation, all safety topics in the approval and Option-B plan. Resolve blocking findings with regression tests; no freeze until zero blocking findings. No production procedure is executed.

## Task 4: New source freeze and full native AMD64 certification

**Files:** Existing packaging/export and certification harness only; inspect the final successful prior run evidence, not the earlier blocked run's counts, before composing the new job.

- [ ] Review exact diff and secret/generated-state hygiene. Commit reviewed local product changes; retain old release evidence unchanged.
- [ ] Use existing sanitized export/build identity machinery to produce new source SHA, file count, manifest, deterministic archive hash and immutable build identity. Include new runtime modules/tests according to the existing export policy; no historical runtime artifacts. Verify dependencies and base-image pins unchanged.
- [ ] Run the full prior 17 backend suites plus new diagnostic cases, retaining required auth overlay, disposable database contracts, network-denied tests, strict source cleanup and reviewed broad diagnostic baseline. Do not claim the broad historical failures are a green whole-repository baseline.
- [ ] Perform production/maintenance container tests, migration/no-op gates, two independent native Linux/AMD64 builds, protected-path checks and exact reproducibility gates. No image push.
- [ ] Independently download the tested OCI artifacts; verify archives, manifests, configs, platform and build identity against certification results. Retain sanitized evidence and artifact expiry information; no publication/deployment.
- [ ] Final report must say either `NEW OBSERVABILITY RELEASE CERTIFIED — PRODUCTION TRANSITION NOT YET AUTHORIZED` or `OBSERVABILITY RELEASE BLOCKED`, with exact evidence and unfinished gates. Never present old image digests as newly certified images.

## Plan review checkpoint

Implementation has not started. Review this concrete plan before execution, preserving the already-approved bounded design. Native sequential implementation followed by fresh independent review is recommended; no parallel implementation changes are needed.
