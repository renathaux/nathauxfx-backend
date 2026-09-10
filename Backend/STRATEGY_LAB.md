# Strategy Lab Phase 1

Strategy Lab is an analysis-only historical replay subsystem. Phase 1 supports
`baseline_v1` for EURUSD with custom UTC start/end dates (maximum 120 days).

## Safety boundary

The package in `services/strategy_lab` reads immutable `indicator_candles` and
strategy settings. It does not import the cTrader connector, event lifecycle,
trade submission, PAPER/LIVE entry, or strategy watch modules. All replay state
and simulated positions live in local Python objects for the duration of one
request. The API never fetches candles from cTrader.

## Time model

- M15 decisions use only candles whose close is at or before the simulated time.
- M5 confirmation must close after the M15 break close and within four M15 bars.
- Entry is the qualifying M5 close.
- Outcomes begin with the following M5 candle.
- When OHLC cannot establish whether an active stop or target was touched first,
  the result is `AMBIGUOUS_INTRABAR`.

## Known approximations

Every response reports these in `diagnostics.unsupported_or_approximated`:

- the durable two-sub-minimum-BOS exception is represented by a chronological
  same-direction approximation;
- historical spread, slippage, and tick ordering are unavailable;
- TP2 uses the production 2R fallback without mutable broker context.

These constraints are disclosed rather than silently guessed.
