# Dedicated DEMO broker integration test

Run from `Backend/` after Alembic revision `20260916_0022` is applied. This CLI has no HTTP route, scheduler, strategy signal, or automatic opening retry. It accepts only DEMO account `47784297`, EURUSD, BUY, and a stable explicit test ID. Account `47810571` is never permitted.

```sh
python scripts/run_broker_integration_test.py --account-id 47784297 --test-id demo-proof-20260916-01 --symbol EURUSD --confirm-demo-broker-test --preflight
python scripts/run_broker_integration_test.py --account-id 47784297 --test-id demo-proof-20260916-01 --symbol EURUSD --confirm-demo-broker-test
python scripts/run_broker_integration_test.py --account-id 47784297 --test-id demo-proof-20260916-01 --symbol EURUSD --confirm-demo-broker-test --recover
```

`--preflight` performs only authentication and broker/database reads; it creates no submission or fence. It requires fresh explicit broker `isLive=false`, hedged full-access account, a non-limited-risk account, zero existing account positions/pending orders, exact EURUSD symbol, full SymbolById minimum/step/maximum volume, a quote younger than 30 seconds, and complete order/deal history availability. Missing/unsupported metadata fails closed. It neither refreshes OAuth credentials nor changes account selection.

The full invocation durably creates the unique test reference and account fence, rechecks preflight, and commits an atomic request-start marker before the one opening request. Volume uses raw protocol cents directly: no lot conversion or hardcoded fallback. Test-only relative SL/TP start at 0.005 EURUSD price distance (50 pips), widened to the broker minimum plus twice the spread, bounded by 0.02. Only point-based distance metadata is accepted. These protections are not a V3B plan or eligibility signal. The broker may still reject them; any marked request remains recoverable and never gets resent.

The service records matched OPEN evidence, refreshes the committed same-ID row and reconciles it again without opening, then closes only the exact matched position and currently proven remaining volume. It requires complete order/deal/position evidence: exact account, reference, symbol, BUY opening direction, order/position identity, and opening volume equal to closed plus remaining volume. Unknown, truncated, mismatched, partial, or missing history cannot release the fence. Merely disappearing from open positions is insufficient. A broker rejection after the marker also stays fenced unless later evidence proves closure; there is intentionally no inference that absence means no dispatch.

Exit code 0 means read-only preflight succeeded or the durable state is CLOSED. Exit code 2 means BLOCKED/NEEDS_RECOVERY; inspect the sanitized JSON output and run the same ID with `--recover`. Recovery never opens an order, including a PREPARED record that never reached its durable dispatch marker. A PREPARED recovery may release its fence because no network submission was entered. A completed ID never opens again. A different ID is refused while any unresolved test exists. No automatic recovery worker runs on startup.

OPEN, duplicate OPEN, and final CLOSED evidence remain in dedicated JSON columns and CLI output. Request/close/reconciliation timestamps and broker IDs are durable; raw broker responses, exception messages, and secrets are not persisted. A network timeout or lost close response leaves the fence in place; recover explicitly. If broker history is not yet complete immediately after acceptance/fill, recovery may be necessary. Never use a new ID to work around the fence.

PostgreSQL uses a nonblocking transaction advisory lock on a dedicated connection across broker work; this supports transaction poolers. A unique nullable unresolved-account column retains fencing across restarts and lock-owner crashes. SQLite uses a nonblocking local file lock for local deployments/tests. Multiple hosts sharing a SQLite file are unsupported. No TTL silently releases unresolved exposure.

Normal submissions share coordination for this account. Runtime account selection is pinned internally through the connector boundary, rejecting a mid-call account switch. Other accounts retain their ordinary path. LIVE Auto preferences and strategy analysis stay unchanged. Exact tracked references/position IDs are excluded from normal position mirrors and closed-deal strategy history, including the interval before the broker position ID is persisted. Broker account balance naturally reflects the test's realized fees/P&L.

Verify current account selection, LIVE Auto ON, and stream status independently before/after release. Closure proof covers this test's position; normal trading may resume immediately when its fence releases. Do not confuse a later unrelated position with test exposure. If cleanup or complete evidence is unavailable, report BLOCKED and keep the fence.

Protocol authority (consulted 2026-09-16): [cTrader model messages](https://help.ctrader.com/open-api/model-messages/) and [request/response messages](https://help.ctrader.com/open-api/messages/). `minVolume`, `stepVolume`, `maxVolume`, position/deal volumes and `closedVolume` are protocol cents; relative protections use 1/100000 price units. `isLive` is optional, so its absence is not DEMO proof.
