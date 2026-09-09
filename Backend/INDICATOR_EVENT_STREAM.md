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
inserts one `trade_submission_attempts` row. The full stable `fs1_...`
idempotency key remains in the database and cTrader label. A deterministic
`fsc1_...` SHA-256-derived reference is used as the cTrader client order ID and
is at most 50 characters. A process restart releases
only a claim whose broker request was never marked started. A possibly sent
request stays `RECONCILIATION_REQUIRED`; it is never automatically resubmitted.

Lifecycle and submission uniqueness is scoped by event, mode, application
owner, and cTrader account. PAPER uses its own `PAPER` account scope. Broker
reconciliation reads open positions, historical orders, and historical deals
separately for each incomplete attempt account and requires matching account,
symbol, direction, stable reference, broker identity, and timing when present.

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

## Deployment execution fence

Migration `20260908_0018` installs execution protocol
`indicator-event-execution-v2`. A new worker refuses to start the forex loop
when the table is absent or its version is incompatible. Because older workers
do not understand this fence, a mixed-version rolling trading deployment is
forbidden. Deployment must occur in this order: turn PAPER off; turn LIVE off;
stop or scale every old trading worker to zero; run the migration/init while no
old trading worker is active; start only new workers; verify the fence and
stream startup; then seek separate approval before enabling either mode.

The public chart route is read-only. It may analyze visible candles for display
swings, but its event overlays are loaded from `indicator_events`; it never
initializes/backfills a stream, advances a watermark, or selects a replay
origin.
