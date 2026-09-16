# Dedicated DEMO broker integration proof

Approved by the user on 2026-09-16. This is a CLI-only subsystem, not a strategy.

## Scope and invariants

- Only explicit account `47784297`, freshly proved DEMO by broker metadata. Account `47810571` is forbidden.
- Require explicit confirmation and stable caller-supplied test ID. EURUSD only, minimum broker-accepted volume, one BUY for plumbing verification.
- LIVE Auto preference stays ON, account selection unchanged. No indicator candles/events/lifecycles or V3B eligibility changes.
- Separate durable `BrokerIntegrationTestSubmission` identity, no foreign key to strategy events. Persist account/scope/environment/symbol/side/volume/reference/state and request/open/close/reconciliation/error evidence.
- Persist request-start before the network call. Once marked, never resend the opening order even after errors, timeout, restart or retry. Reconcile only.
- Only one unresolved test per account. A durable account execution fence survives restarts; unresolved or ambiguous outcomes retain it. Normal submission for this account must honor it atomically; analysis continues and other accounts are unaffected.
- Release coordination only after complete broker evidence proves the exact position closed (or proves no dispatch ever occurred). Absence in an incomplete response is never closure proof.
- Close only the exact positively matched test position with correct account, symbol, side, reference and broker volume; never close an unrelated position.
- No dashboard invocation or periodic opening orders. CLI retry/recovery is explicit, closes/reconciles only after dispatch. Startup must preserve the durable fence even if no operator is present.
- Existing V3B guards remain unchanged except the additional narrow test fence. No normal strategy state edits or fake eligibility.

## Architecture

Dedicated SQLAlchemy table and Alembic migration; small state-machine service, account execution coordination helper, cTrader test adapter and CLI. Use short durable transactions for state transitions and a cross-process per-account lock for mutual exclusion spanning broker operations. Account fencing is persistent, not a TTL. A crash releases process ownership, never the durable unresolved-test fence. A restarted invocation of the same ID resumes reconciliation/cleanup; a different ID is refused.

The broker adapter pins credentials/config to the verified account and DEMO endpoint; no fallback symbol metadata or volume guesses. It obtains current bid/ask, minimum/step/max volume, existing positions/orders, and account metadata. A dedicated tracked send may use existing socket/payload helpers but never bypass durable service orchestration. Broker-side protection and synchronous cleanup are required; failure evidence is retained without secrets.

## Verification

TDD: wrong account/live/missing confirmation/missing account; identity before network; one unresolved test; duplicate calls/restart never resend; successful OPEN match then CLOSED match; ambiguous errors; cleanup outage retains fence; normal execution honors lock without changing preference or analysis; no strategy persistence; migration upgrade; normal focused safety suite.

Release through isolated PR, green CI, merge, Render auto-deploy exact SHA. Production preflight before mutation, exactly one order, repeat same-ID read/reconcile without another send, immediate close and final reconciliation. Any uncertainty stops new submissions. If cleanup is unavailable, retain fence and report BLOCKED instead of claiming successful cleanup.
