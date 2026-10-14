# Runtime identity diagnostic

`GET /admin/runtime-identity` reports observations from the serving application process. It does not run a diagnostic subprocess, start recovery or establish authority. The hosted verifier flag does not gate this endpoint.

Authentication uses the existing SELECT-only `read_admin` or `read_legacy_owner` predicate within a PostgreSQL-enforced read-only transaction. Modern `FlowSignalUser` headers or `flowsignal_session` cookies require `X-FlowSignal-CSRF`; legacy owner tokens use the existing `Bearer` header contract only. Invalid, expired or non-admin sessions are rejected. There is no cache fallback, refresh or cleanup. Responses, including failures, carry `Cache-Control: no-store`. No token, email, database URL, arbitrary exception text or broker credential is returned.

## Schema: `flowsignal-runtime-identity/v1`

| Section | Meaning |
| --- | --- |
| `process` | Current process PID, kernel-derived start timestamp where available, bounded `RENDER_INSTANCE_ID` if available. Unavailable start/instance evidence is null. |
| `startup` | Allowlisted build hashes, generated boot UUID, committed attempt UUID/epoch/creation timestamp, submission-reconciliation outcome, acquisition attempt/result, token installation, producer registration, outcome/reason, phases and completion timestamp. Detached copies; historical record freezes when bootstrap returns. |
| `observed_durable_owner` | Owner UUID/epoch copied from an existing startup row read or successful acquisition commit, with observation time. Null UUID may mean an observed vacant owner only when an observation timestamp exists. This is not a fresh database query or current ownership proof. |
| `runtime_observation` | Timestamped local LIVE boolean (strict bool only), installed API-runtime token presence, producer-admission observation and execution-active observation. Not a global census of tokens in all threads. Missing API state is `UNKNOWN`. |
| `authority_observation` | Timestamped `broker_mutation_authority: UNKNOWN` and `REAL_ORDER_DISPATCH_AVAILABLE: false`. The diagnostic has no dispatch capability. |
| `captured_at` | Response capture time, not owner freshness or a lease. |

Producer admission is false when no writer/runtime is installed; a present writer is **not** sufficient proof of current admission and remains `UNKNOWN`. Historical `startup.checkpoint_producer_admitted: true` means the existing registration call returned successfully, not that it remains admitted. Execution-active and current broker mutation authority remain `UNKNOWN`: this endpoint deliberately does not invoke database fencing or broker checks to prove them.

Allocation identity is published only after the existing allocation transaction exits successfully. An uncertain commit leaves committed identity unknown even if a token was constructed. Acquisition distinguishes `COMMITTED`, `REJECTED` and `UNKNOWN`; an uncertain commit never becomes a successful acquisition or a definite rejection. Failures before acquisition do not become owner-busy rejections. Startup may remain incomplete while in progress. Recording failures are suppressed without logging payloads and cannot authorize or change recovery.

The exact GET route is excluded from recovery-context middleware capability evaluation; it still performs its own mandatory read-only authentication. All other methods/paths retain existing middleware behavior. Normal startup and trading routes are unchanged.

Repeated reads do not mutate the recorder or runtime state. Snapshots are detached under a lock. Forked processes do not reuse the parent's recorded identity or wait on a parent-inherited lock. No timestamp is used to reconstruct a boot UUID, attempt, epoch or authority.

## Certification and operational limits

Local tests are supplemental. Native Linux/AMD64 full release certification and independent review are required before a new freeze can become deployable evidence. Historical BLS default backfill remains unsupported for this release; the CLI's presence does not change that boundary. No production action is authorized by this diagnostic or its certification.

Gold LIVE authority remains Strategy Studio / virtual Studio → FlowSignal's real production trading pipeline. This endpoint changes no strategy, routing, risk, protection or execution behavior.
