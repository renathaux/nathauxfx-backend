# Strategy Lab Phase 1

Strategy Lab is an analysis-only historical replay subsystem. Phase 1 supports
`baseline_v1` for EURUSD with custom UTC start/end dates (maximum 120 days).

## Safety boundary

The package in `services/strategy_lab` reads immutable `indicator_candles` and
strategy settings. It does not import the cTrader connector, event lifecycle,
trade submission, PAPER/LIVE entry, or strategy watch modules. All replay state
and simulated positions live in local Python objects for the duration of one
request. The API never fetches candles from cTrader.

Both `/strategy-lab/strategies` and `/strategy-lab/replay` require the existing
FlowSignal administrator session, including the established owner-session
compatibility path. There is no Strategy Lab-specific secret. Authentication
aside, replay candle access issues SELECT queries only.

## Time model

- M15 decisions use only candles whose close is at or before the simulated time.
- M5 confirmation must close after the M15 break close and within four M15 bars.
- Entry is the qualifying M5 close.
- Outcomes begin with the following M5 candle.
- When OHLC cannot establish whether an active stop or target was touched first,
  the result is `AMBIGUOUS_INTRABAR`.

## Known approximations

Every response reports these in `diagnostics.unsupported_or_approximated`:

- historical spread, slippage, and tick ordering are unavailable;
- runtime broker position state is represented by isolated simulated trades.

The exact internal two-BOS qualification is replayed in memory. TP2 uses the
nearest qualifying opposing valid M15 swing and applies the production 2R
fallback only when no swing satisfies the configured RR window.

These constraints are disclosed rather than silently guessed.
