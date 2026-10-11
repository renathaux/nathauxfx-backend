# Legacy checkpoint adoption — local implementation contract

This document is not production authorization. No successor image, production
migration, handoff, or recovery operation is performed by this change.

## Preconditions and explicit release input

The receipt-aware release requires PostgreSQL schema `20261010_0031`. Migration
0031 adds only `recovery_legacy_adoptions`; it does not backfill authority.
The normal migration runner and ownership primitives are unchanged.

The server composition accepts two explicitly provisioned operator settings:

- `FLOWSIGNAL_ADOPTION_MANIFEST`: absolute installed `.cutover/<digest>.json` path.
- `FLOWSIGNAL_ADOPTION_MANIFEST_SHA256`: independently retained approved digest.

These are cutover evidence, not build identity. Neither is discovered from the
state directory. A missing pin blocks an eligible legacy adoption. The configured
root must match all five reviewed runtime paths. Provisioning either production
setting requires separate approval. The administrative operator remains trusted;
an attacker able to replace both expectations and evidence is outside this pin's
protection.

## Recovery sequence

Normal owner acquisition → database/authentication → stable read-only discovery
→ `STATE_DISCOVERED` → eligible legacy adoption → fresh discovery → unchanged
normal reconciliation. Compatible existing heads bypass adoption. A blocked owner
cannot resume through this hook.

The transported seven files and original `.cutover` metadata remain immutable.
The exact `legacy_adoption._transport` call site consumes shared read-only
`cutover_evidence.read_original_transport` with the explicit manifest path and
independent pin. Startup cannot invoke the cutover command/service or
`verify_state`. Original transport proof is not an explanation of extra state:
adoption separately validates its complete generation and accounts for every
destination entry. Ordinary cutover verification still rejects such extra state.
Five supported kinds are validated together and written as one durable canonical
generation. Only one fenced transaction may publish its receipt and all five
heads. `paper_backup` and `visits` stay quarantined. PENDING news remains PENDING,
unconsumed, and unexpired; unsupported variants block rather than being repaired.
Historical/cache observations never become accepted execution or broker truth.

The adoption binding and final receipt use separate domain-separated hashes;
there is no receipt/envelope hash cycle. An adoption result grants no producer,
management, or entry admission. Those remain later normal recovery gates.

## Retry and ambiguous outcomes

The ordinary cutover verifier still rejects unmanifested recovery generations.
Only the adoption retry validator accounts for the additional namespace:

- COMMITTED_COMPLETE: the original receipt and exact five heads validate.
- UNCOMMITTED_ORPHAN: no receipt/heads, exactly one complete generation bound to
  the same current attempt and all original evidence. Fresh gates precede reuse.
- PARTIAL_OR_CONFLICTING: every other state blocks. No newest-generation search,
  overwrite, automatic repair, or orphan discovery by normal checkpoint readers.

A commit exception reports a sanitized classification; it does not retry. Later
runtime heads use ordinary recovery, not relaxed adoption-retry acceptance.
Never delete unexplained generations to force this gate to pass.

## Termination-only administrative boundary

`startup_recovery.owner_handoff.relinquish_terminated_owner` is an explicit local
administrative API, not startup/HTTP logic. Run in a fresh administrative process
only after independently establishing predecessor termination. It accepts:
exact scope, attempt, epoch, boot/build, deployment, successor release identity,
canonical evidence bytes, an independently approved SHA256, and a server-owned
read-only safety-reader capability. No production evidence is generated here.

Evidence schema 1 contains `kind=operator-termination`, scope, attempt_id, epoch,
boot_id, build_id, deployment_id, successor_release_identity,
`predecessor_status=TERMINATED`, `operations_reconciled=true`, and the retained
platform termination record's SHA256. The independent pin must come from review,
not from blindly hashing untrusted input. This API validates bindings; it cannot
authenticate a fabricated platform record merely because a boolean says true.

The safety reader returns fresh complete database/broker observations twice;
its `database(session, scope)` method rechecks using the fenced transaction.
LIVE must be OFF, selection unchanged, and selected-account exposure/pending or
unresolved operations absent. Exact blocked owner identity and fresh evidence
are mandatory. Old handoff evidence cannot be reused.

Only existing `store.relinquish` is called. No acquire, takeover, attempt creation,
epoch advance, blocker clearing, or process revocation reset occurs. A separately
authorized normal startup can allocate N+1 after relinquishment. The predecessor
process's token remains unusable. An ambiguous commit requires read-only inspection,
not a blind retry. No manual SQL fabrication is part of the procedure.

## Local verification boundaries

Tests use synthetic payloads, disposable PostgreSQL, Linux-owned storage with
usable O_NOATIME, and network-denied test processes. Strict filesystem assertions
are not replaced with macOS semantics or timestamp restoration. No production
credentials, captured JSON, broker orders, deployments, or images are test inputs.

### Task 7 review handoff (not certification)

Focused gates: 195 passed, zero failures/errors/skips. Exact bounded regression:
785 passed, 2 failed, zero errors/skips; all 321 cutover tests passed. Linux
source/test bytes matched all 481 local exported Python files. Tests ran as
UID 501:GID 1000 on Linux ext4 with O_NOATIME, disposable PostgreSQL through
0031, and a network-denied test process.

Remaining failures in `tests/test_database_configuration.py`:

- `DatabaseConfigurationTests::test_local_sqlite_source_is_readable_for_one_time_copy`:
  ISOLATED_EXPORT_MISSING_FIXTURE. The intentionally excluded
  `Backend/database/flowsignal.db` does not exist in the export. No runtime DB
  was imported or fabricated to make this check pass.
- `DatabaseConfigurationTests::test_models_match_existing_sqlite_tables`:
  ENVIRONMENT_FAILURE. The updated model registry assertion passes, but the
  subsequent legacy-table assertion inspects the isolated `sqlite:///:memory:`
  engine, not a provisioned legacy SQLite schema. Independent inspection found
  zero tables. This is not a green full-regression result.

Remaining PRODUCT_REGRESSION and TEST_EXPECTATION_OBSOLETE counts: zero.
The obsolete 0030 fixture/head/model expectations were corrected, not skipped.
The missing Git-index fixture was supplied in the disposable export using only
explicit Python paths, without source history or runtime artifacts.

The final independent whole-change review has deliberately not run. No release
certification or production readiness is claimed by this handoff.
