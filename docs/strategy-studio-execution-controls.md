# Executable Studio controls

Schema version 1 remains readable. Normalization adds structure_timeframe equal to trading_timeframe, stop_loss.distance_filter disabled, and confirmation.max_setup_age_bars null. Existing saved rows are not rewritten. No activation, LIVE Auto, broker submission, or one-position safety behavior changes.

Structure timeframe supports 5m/15m/1h, equal to or higher than trading timeframe. Bundle candles are open-stamped. A higher-frame structure event and its break-validation candle become available on the trading bar ending at the structure bar's close. Confirmations use subsequent trading candles; the setup's invalidation swing comes from the selected structure analysis. Equal-frame behavior remains unchanged.

SL distance filtering computes the normal entry and stop first and never moves the stop. PERCENT_ENTRY = abs(entry - stop) / entry * 100; PIPS divides by the symbol pip size. Minimum/maximum are inclusive (small floating-point boundary tolerance). Failed gates appear in the stop_loss funnel stage as SL_DISTANCE_BELOW_MINIMUM or SL_DISTANCE_ABOVE_MAXIMUM, before targets and risk.

Freshness counts actual closed trading candles after the original event (not elapsed minutes). The event bar is age zero; age N is valid, N+1 returns SETUP_EXPIRED and clears the setup. Remember BOS and same-direction remembered events do not reset age. Continuation carries the bar count and timestamp watermark; duplicate evaluation does not advance it.

Tests: Backend/tests/test_strategy_execution_controls.py covers neutral migration, real simulator trade-set and funnel changes, 4300/4280 = 0.465116%, pip bounds, expiry, Remember BOS, chunk continuation, real 5m vs 15m structure, closed-candle availability and simulator/LIVE evaluator parity. No broker calls are needed.

Gold 832 acceptance: manually clone Gold 831, use XAUUSD, risk PERCENT_BALANCE 1, filter enabled, PERCENT_ENTRY minimum 0.40 maximum 0.60. Keep other rules, structure timeframe and freshness unchanged for a controlled comparison, then use identical five-year dates. Do not activate it merely to backtest. Synthetic fixture: baseline four trades; filtered one trade, ten below-minimum setup rejections. The actual five-year Gold 831 comparison is a separate user run.
