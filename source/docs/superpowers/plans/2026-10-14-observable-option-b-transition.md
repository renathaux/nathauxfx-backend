# Option B: controlled maintenance isolation — PLAN ONLY

Status: proposed for independent review. This document authorizes no production action. The observability release must first pass whole-change review and full native AMD64 certification. Image publication and each production transition require separate authorization. LIVE remains OFF throughout.

## 1. Fresh preflight (separate authorization required)

- Independently pin the certified production and maintenance image digests, configs, build identity and source manifest; do not substitute tags or rebuild.
- Verify the actual active deployment, service, disk, process inventory, effective configuration and immutable image identity.
- Freshly verify runtime and durable LIVE OFF (unknown is blocking), account identity, zero current positions/pending orders, and no unresolved/in-flight submissions. Assess any other account exposure and offline management requirements explicitly; do not change accounts or orders as a diagnostic convenience.
- Read and independently pin the exact current owner token, account/attempt phases, accepted manifest, handoff history, receipt/head/generation authority and canonical unsettled-state evidence required by the certified successor-runtime adapter. Recheck actual allocated epoch; do not presume it is 6 or 7 from historical notes.
- Preserve sanitized, timestamped off-host evidence. Any drift, inaccessible proof or contradictory observation stops the transition before mutation.

## 2. Maintenance isolation, without relinquishment

After separate deployment authorization, change only the same service image to the exact approved maintenance digest, retaining the same disk and reviewed environment. Do not invoke recovery, migration, owner handoff, adoption or checkpoint publication.

Require maintenance health, UID:GID 501:1000, actual mounted `/state`, expected owner/mode, and populated-state acceptance without repair. Verify no application/recovery workers, automatic migration, automatic owner acquisition or broker-dispatch activity. Maintenance HTTP cannot dispatch; administrative access must not execute broker-write or recovery commands. Credentials being present are not proof that an HTTP process used them.

**Stop:** launcher rejection, unexpected filesystem mutation, incorrect image, absent mount or missing administrative visibility. Do not chmod/chown, repair state, restart production or fall back to an old owner token automatically. Retain durable ownership unchanged while the failure is assessed.

## 3. Prove all predecessors terminated

Collect trusted deployment deactivation and instance/process termination evidence for every predecessor capable of running the application, including old workers/replicas. Correlate actual instance/process identity with the durable owner and retained boot/build evidence. Load-balancer removal, a new deployment serving traffic, an old HTTP URL failing, or elapsed time alone is insufficient.

**Stop:** any predecessor remains alive, its termination cannot be proved, or identity cannot be bound. Do not relinquish on an inference. Re-read DB: epoch-6 ownership must remain unchanged until the separately authorized transaction (if current DB differs, stop and reassess rather than force epoch 6).

## 4. Separately reviewed relinquishment

Construct new termination/graceful-drain evidence using the applicable certified adapter, fresh safety observations and the exact independently pinned current authority shape. Validate it without mutation first. Review adapter choice and evidence separately; old epoch handoff evidence is not reusable authority.

Only after explicit authorization execute the one sanctioned relinquishment transaction. No manual SQL authority changes. Read back the durable result; an ambiguous outcome requires read-only classification and a stop, never a blind retry. Preserve receipts and audit evidence. Do not create a successor attempt through administrative functions.

## 5. One observable successor startup

Before deployment, require reviewed evidence of the intended instance/replica count, worker count, automatic-deployment settings and platform restart behavior. One operator deployment is not proof of one process startup. If those controls cannot assure one observable successor startup, stop before deployment and obtain a revised, separately approved procedure; do not discover the limitation by creating attempts. This plan does not authorize inspecting or changing production controls now.

After separate production deployment authorization, deploy the exact new certified production image once. Let the normal certified allocator choose its next epoch. Do not force or reuse epoch 7, rewrite the allocated counter, restore old tokens, call recovery/adoption manually, or repeatedly redeploy to obtain a preferred outcome.

Immediately inspect authenticated `/admin/runtime-identity` on the actual serving process. Correlate process/instance, immutable build, boot UUID, committed attempt UUID/epoch, actual acquisition result and final startup facts with independently read durable authority. A startup observation or matching owner is not current broker-write authority. `UNKNOWN` remains unknown and may block the required proof.

**Stop:** busy rejection, ambiguous commit, incomplete identity, multiple unexpected attempts, timeout, recovery block, mismatch or unexplained state. Preserve fail-closed runtime/DB evidence. Any maintenance fallback needs fresh safety review and separate deployment authority; it does not revoke or replace ownership automatically.

## 6. Readiness and PRE-ENABLE, not LIVE enablement

Validate current checkpoint/receipt/generation authority through the certified read-only path, including successor generations as supported by the applicable adapter. Recheck account/broker exposure and unsettled submissions independently, plus runtime and durable LIVE OFF.

Only after separately authorized hosted PRE-ENABLE execution, require the real hosted diagnostic to return `WOULD_ALLOW` with diagnostic dispatch unavailable. This does not enable LIVE or submit an order. If evidence is stale, unavailable, contradictory or blocked, stop without clearing blockers.

Final stop: LIVE OFF; no real order test. Any later LIVE authorization must preserve Gold execution through certified Strategy Studio / virtual Studio and the real FlowSignal trading pipeline.

## Rollback constraints

- Before relinquishment: preserve the durable owner; never infer release from process termination.
- After relinquishment: do not resurrect the old owner; a future process must use the allocator and certified acquisition.
- After successor acquisition/failure: do not restore the predecessor's token or decrement epochs; new maintenance/handoff actions require fresh evidence and approval.
- At every ambiguous transaction: read-only inspection, retain evidence, no retry or manual repair.
- No automatic production rollback/redeploy is authorized by this plan.
