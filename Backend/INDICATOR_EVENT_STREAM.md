# Authoritative indicator event stream

Phase one keeps the current `legacy_engine` BOS/CHoCH rules and moves their
confirmed output into a durable event stream. Closed candles are appended to
`indicator_candles`; the engine replays from that immutable origin; confirmed
events are stored in `indicator_events`. Existing candle and event rows are not
rewritten when a chart requests a different range.

An event ID hashes only stable indicator identity: symbol, timeframe, event
candle timestamp, BOS/CHoCH classification, direction, broken level, and the
event-owned opposite swing. LIVE/PAPER status and the M5 confirmation are stored
separately in `indicator_event_lifecycle`.

## Lifecycle and submission safety

`WAITING`, `BLOCKED`, and `ELIGIBLE` are temporary. `SUBMITTING` and
`RECONCILIATION_REQUIRED` are protected in-flight states. `CONSUMED`,
`EXPIRED`, and `INVALIDATED` are terminal. General strategy reevaluation cannot
move an in-flight or terminal row back to a temporary state.

Before LIVE calls cTrader, it atomically changes `ELIGIBLE` to `SUBMITTING` and
inserts one `trade_submission_attempts` row. The stable `fs1_...` idempotency
key is sent as cTrader's client order ID and label. A process restart releases
only a claim whose broker request was never marked started. A possibly sent
request stays `RECONCILIATION_REQUIRED`; it is never automatically resubmitted.

## Startup and candle corrections

Startup initializes all EURUSD/XAUUSD 5m, 15m, and 1h streams before starting
the trading loop. Backfilled events are marked historical through the startup
activation watermark and cannot become strategy candidates.

Identical duplicate candles are idempotent. A previously missing late candle
is inserted and replayed. If it changes any immutable accepted event, the
stream enters `RECONCILIATION_REQUIRED`. Conflicting OHLC for an already stored
closed candle also enters reconciliation. Short missing intervals block
watermark advancement; known venue and weekend closures are not inferred as
missing candles.

## Retention/checkpoint policy

Canonical candles are retained for the lifetime of the current legacy replay
configuration. Events referenced by lifecycle or submission records are never
deleted. Automated destructive retention is disabled until a checkpoint stores
the complete analyzer state plus its origin/configuration hash and a replay
test proves that events after the checkpoint equal full-origin replay. Database
backups or archival replicas may compact cold storage, but the active canonical
rows must remain available. This policy favors reproducibility over premature
deletion; storage growth must be monitored before a checkpoint migration is
introduced.

The chart, LIVE, and PAPER paths consume these persisted events. EMA,
consolidation, M5 confirmation, expiry, risk, position, and broker gates remain
downstream qualification rules.

Phase one deliberately retains all current risk behavior. In particular,
XAUUSD keeps `XAUUSD_SL_BUFFER_QUOTED_POINTS = 500`; with the current `0.01`
point size this is a `$5.00` price buffer beyond the event-owned M15 swing.
