# Relinquishment observation scheduling

## Authority and scope

User authorizes end-to-end timing redesign without routine approval pauses. Preserve the two reviewed invocation-local optimizations. No deadline, strategy, risk, execution, ownership, checkpoint or reconciliation semantics change.

## Alternatives

Prevalidate lineage then rebind pointers: rejected for this correction. Content-addressed generation names are not filesystem immutability; files are writable and writers do not uniformly participate in the verification shared lock. Safe reuse would need a larger proof and fresh byte validation.

Shorten canonical inputs: rejected. The 3.4MB snapshot is part of the current safety comparison contract; removing fields could weaken identity checks.

Selected: a certified administrative-only concurrent observation reader. Each invocation freshly validates credentials/account selection, then overlaps a complete read-only DB snapshot on a separate connection with complete broker reads in the caller thread. Join before returning. The next observation cannot begin until the prior observation is complete. All downstream comparisons, frozen snapshots, locked reread, lineage, reconciliation and final freshness checks remain unchanged.

## Dependency graph and concurrency boundary

No ownership transaction → fresh credentials/selection → [DB snapshot worker || caller-thread complete broker observation] → join → observation return → safety comparison → second complete observation → filesystem/account/owner locks → fresh DB reread → full authority/reconciliation → final20s check → unchanged relinquish exactly once.

No broker/network callback while holding ownership locks. Reject an active recovery transaction before dispatch. Copy caller context to the DB worker, never share a SQLAlchemy Session/Connection across threads. The database reader owns its independent read-only connection. Broker socket stays in its caller thread. A per-reader nonblocking single-flight lock rejects concurrent calls. No cache or prepared authority crosses calls. All workers finish before return/error; failure cannot proceed to mutation. Broker protocol timeouts and PostgreSQL read-only query/lock timeouts remain enforced.

The20s clock still starts before the first observation in the existing runtime adapter. Reader additionally rejects an expired complete observation. No timer reset, fewer observations, omitted history, or weakened validation.

## Equivalence and race safety

Concurrency alters scheduling only. Both complete snapshots remain fresh; account selection is checked by both credential and DB paths. Both observations and final locked DB reread must retain existing canonical identity equality. Owner, heads, receipts, generations, provenance, termination document, LIVE and unsettled operations remain freshly validated by the unchanged adapter path. No precomputed authority is introduced, so stale/cross-request precomputation reuse is impossible by construction and retries reread everything.

## Verification

Prove actual overlap with synchronization barriers, not timing sleeps. Prove join-on-error, no worker escape, caller-thread broker, transaction rejection, single-flight, full history forwarding, fresh read per call, expired observation rejection. Exercise adapter adversarial regressions and exact one-write success in disposable PG. Compare sequential/concurrent snapshots and full safety decisions over equivalent synthetic state. Run full existing suites plus new tests. Use production-calibrated latency profiles with real disposable locking and full validation; distinguish timing simulation from actual deployment evidence. Do not accept on faster unit tests alone.

Independent review and full native AMD64 recertification/reproducibility precede publication or production transition. Production remains maintenance-held until certification and fresh preflight pass.
