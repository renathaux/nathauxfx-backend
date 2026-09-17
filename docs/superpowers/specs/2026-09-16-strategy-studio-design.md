# NathauxFX Strategy Studio Design

Date: 2026-09-16
Status: Approved design, implementation not started
Scope: Strategy Builder + Simulator + future LIVE handoff

## 1. Purpose

Strategy Studio lets a user build trading strategies without writing code. The user chooses from a controlled set of trading rules and may type numeric values where numeric freedom matters.

The system must stay simple for the user while keeping one structured strategy definition underneath. The same strategy evaluator must eventually power both LIVE trading and simulation so the same closed-candle sequence and the same strategy settings produce the same decision.

The existing production V3B path remains untouched until Strategy Studio reaches proven parity with it.

## 2. Product principles

1. Fixed rule selections, not arbitrary code, natural-language strategy generation, or a node graph.
2. Numeric inputs remain flexible where appropriate. The app guides users but does not impose a trading style.
3. Fields appear only when relevant to the selected parent option.
4. One global active strategy at a time.
5. Only one cTrader account is active for LIVE execution/management at a time.
6. The active strategy follows the currently selected LIVE account.
7. Simulator activity never changes LIVE strategy state.
8. LIVE and Simulator must ultimately use the same strategy evaluator.
9. Sensitive actions always require confirmation.
10. Strategy Studio is introduced in stages so the current working V3B path is not destabilized.

## 3. Strategy Studio layout

The Strategy Studio page has three main areas.

### 3.1 Saved Strategies panel

A left-side list shows saved strategies. Each card shows:

- strategy name
- state: Active, Inactive, or Locked
- symbols: EURUSD, XAUUSD, or Both
- trading timeframe
- risk method
- concise summary, for example `5m • BOS/CHOCH • 1% risk`

Actions include:

- New Strategy
- Clone
- Activate / Deactivate
- Delete
- Simulator

New Strategy always opens a blank builder. There are no templates in the first version.

### 3.2 Strategy Builder

The center panel contains fixed sections. It uses dropdowns, multi-selects, toggles, and conditional numeric inputs.

Missing or invalid values are shown directly under the relevant setting. A new strategy cannot be saved until every required field is valid.

### 3.3 Strategy Summary

The right-side panel updates live while the user edits, including before Save.

It presents the strategy as a readable flow rather than raw configuration. Example:

`1h trend -> 5m BOS/CHOCH -> 50% body -> next 5m same direction -> confirmation-close entry -> 5m swing SL + 5 pip buffer -> TP1 0.75R / close 80% / protect +0.2R -> TP2 2R -> risk 1%`

Incomplete required steps are visibly marked incomplete.

## 4. Strategy definition

Each strategy is stored as one structured configuration. It contains at least:

- strategy ID
- name
- selected symbols
- trading timeframe
- optional trend timeframe
- selected trend filters
- break validation selections and values
- entry confirmation selections and values
- entry method
- stop-loss method and values
- TP1 enabled/disabled and values
- TP2 method and values
- risk method and value
- active/inactive state
- created/updated timestamps

Custom numeric inputs store the actual numeric value, not merely the label `Custom`.

No arbitrary executable code is stored in a strategy.

## 5. Symbols

A strategy supports:

- EURUSD
- XAUUSD
- Both

At least one symbol is required.

If Both is selected, the same strategy logic and the same numeric strategy settings apply to both symbols.

Risk sizing is still calculated independently per symbol because each setup can have a different SL distance. For example, one $100 risk setting may produce different lot sizes on EURUSD and XAUUSD.

## 6. Trading timeframe

Fixed options:

- 5m
- 15m
- 1h

The trading timeframe automatically controls:

- BOS/CHOCH evaluation
- Last Swing SL timeframe
- trading-timeframe confirmation logic unless a rule explicitly refers to the optional trend timeframe

The user does not separately select a BOS/CHOCH timeframe or swing-SL timeframe.

## 7. Optional trend filter

Trend filtering may be None.

If enabled, Trend Timeframe must be higher than Trading Timeframe.

Allowed Trend Timeframes:

- Trading 5m -> None, 15m, 1h, 4h
- Trading 15m -> None, 1h, 4h
- Trading 1h -> None, 4h

### 7.1 Trend methods

The user may multi-select:

- BOS/CHOCH direction
- EMA 50
- EMA 200
- Swing Structure
- All

`All` is a convenience selection for all available trend methods.

If multiple methods are selected, they use AND logic: every selected trend filter must agree with the potential trade direction.

Swing Structure is defined simply as:

- Higher Highs + Higher Lows -> bullish
- Lower Highs + Lower Lows -> bearish
- mixed/unclear -> no trend confirmation

If Trend Filter is None, all trend-specific fields are hidden and trend filtering is skipped.

## 8. Structure trigger

The structure trigger is BOS/CHOCH as one combined trigger type.

BOS and CHOCH are not separately configurable because they feed the same downstream strategy behavior:

- bullish BOS/CHOCH -> potential BUY setup
- bearish BOS/CHOCH -> potential SELL setup

The trigger always uses the Trading Timeframe.

## 9. Break Validation

Break Validation is multi-select and uses AND logic when more than one rule is selected.

Options:

- None
- Candle close beyond broken level
- Minimum candle body %
- Minimum distance beyond broken level

### 9.1 Minimum candle body %

Presets:

- 40%
- 50%
- 60%
- Custom

Custom allows any valid percentage input.

### 9.2 Minimum distance beyond broken level

Presets:

- 5 pips
- 10 pips
- 20 pips
- Custom

Custom allows any valid numeric value. Symbol-specific pip/point conversion remains an engine responsibility.

## 10. Entry Confirmation

Entry Confirmation is optional and multi-select. If more than one confirmation is selected, every selected confirmation must pass.

Options:

- None
- Next candle closes in the same direction
- Second close beyond the broken level
- Retest broken level
- Minimum candle body %

Minimum candle body % uses the same 40% / 50% / 60% / Custom pattern.

## 11. Entry Method

Fixed choices:

- At BOS/CHOCH candle close
- At confirmation candle close
- At retest of broken level

Only context-valid choices are enabled. Examples:

- confirmation-close entry requires at least one confirmation rule
- retest entry requires a retest condition

## 12. Stop Loss

Stop Loss is required for every strategy.

Methods:

- Last Swing
- Fixed Pips / Points

### 12.1 Last Swing

The swing automatically comes from the Trading Timeframe.

Optional buffer:

- None
- 5 pips
- 10 pips
- Custom

### 12.2 Fixed Pips / Points

The user types the numeric distance.

No ATR stop is included in the first version.

## 13. TP2

TP2 is the real final take-profit and is required for every strategy.

Methods:

### 13.1 Fixed R

Presets:

- 1R
- 1.5R
- 2R
- Custom

### 13.2 Fixed Pips / Points

The user types the numeric distance.

### 13.3 Opposite Swing

The engine resolves the relevant opposite swing from the Trading Timeframe.

TP1 cannot exist without TP2. Because TP2 is mandatory, this condition is structurally guaranteed.

## 14. TP1 and protection

TP1 is optional.

If TP1 is disabled:

- there is no partial close
- there is no TP1 protection
- the full remaining position runs toward TP2 or SL

If TP1 is enabled, protection is automatically required. There is no separate Protection On/Off toggle.

### 14.1 TP1 target

Presets:

- 0.5R
- 0.75R
- 1R
- Custom

### 14.2 TP1 close percentage

Presets:

- 50%
- 70%
- 80%
- Custom

Custom must be greater than 0% and no greater than 100%.

### 14.3 TP1 protection

Presets:

- Breakeven
- +0.2R
- +0.4R
- Custom

Custom stores a user-entered R value.

## 15. Risk

Risk Method is required.

Methods:

- Risk %
- Fixed $ Risk

Fixed Lot Size is intentionally excluded.

### 15.1 Risk %

The user may type any valid percentage greater than zero.

Risk is calculated from the currently selected cTrader account balance, not equity.

### 15.2 Fixed $ Risk

The user types any valid dollar risk amount greater than zero.

### 15.3 Position sizing

NathauxFX calculates lot size from:

- chosen risk
- entry
- SL distance
- symbol-specific contract/pip/point rules

For Both symbols, the risk setting is entered once and the app calculates the appropriate lot size independently for each symbol.

Broker constraints do not invalidate the saved strategy. A specific trade can still be blocked at runtime if, for example, calculated volume is below broker minimum or the SL violates broker minimum-distance rules. The UI must show the exact broker/execution reason rather than silently changing the strategy.

## 16. Removed sections

The first version intentionally has no:

- Session Filter
- Extra Filters
- spread filter
- news filter
- volatility filter
- cooldown/max-trades builder section

These are out of scope unless explicitly added later.

## 17. Save, edit, clone, rename, and delete

### 17.1 Save

Save updates the same strategy. Saving does not automatically create a new version.

A new strategy cannot be saved until its required setup is valid.

### 17.2 Clone

Clone is always allowed, including when the source strategy is active or locked.

The clone copies the source strategy exactly, including:

- symbols
- timeframes
- filters
- confirmations
- entry settings
- SL/TP settings
- risk values

The clone starts inactive and editable and can be renamed immediately.

### 17.3 Rename

Inactive/editable strategies may be renamed. A strategy locked by an active trade on the selected account cannot be renamed.

### 17.4 Delete

Delete is permanent after confirmation. There is no archive or trash because the product should not retain unnecessary deleted strategy data.

## 18. Active strategy behavior

There is only one global active strategy at a time.

Activating another strategy prompts for confirmation. After confirmation:

- previous strategy becomes inactive
- new strategy becomes active

The active strategy follows whichever cTrader account is currently selected for LIVE.

Example:

- Account A selected + Strategy 1 active
- user selects Account B
- Strategy 1 remains the active strategy
- Account B becomes the only account receiving new automated decisions/trades

The Simulator is independent and may run any saved strategy while a different strategy is active in LIVE.

## 19. Account-scoped management behavior

Only the currently selected cTrader account is actively analyzed/executed/managed by NathauxFX for LIVE.

### 19.1 Switching away with an open trade

Account switching remains allowed even if the current account has an open trade.

Because this is sensitive, the app must show a confirmation such as:

> Open trade detected. Switching accounts will stop NathauxFX management of this trade. The trade will remain open on cTrader with its current broker SL/TP. Continue?

If confirmed:

- the old account becomes inactive to NathauxFX
- the position remains on cTrader
- NathauxFX stops app-side management for that inactive account
- existing broker SL/TP remain in force
- the newly selected account becomes the only actively managed LIVE account

A valid setup on the newly selected account may open a new trade even if the old inactive account still has an open cTrader position.

### 19.2 Switching back to an account with an open trade

When the user returns to an account and the cTrader position is still open, NathauxFX resumes management using the currently active strategy and current broker position state.

On reactivation, NathauxFX immediately checks current live price against TP1.

If current price is already beyond TP1:

- execute the configured TP1 partial close immediately at current market price
- do not retroactively apply a protection move that was missed while the account was inactive
- existing broker SL remains unchanged
- TP2 remains in force

If TP1 was crossed while inactive but current price has returned below TP1 before reactivation:

- do nothing retroactively
- no partial close
- no protection

If price later reaches TP1 again while the account is active:

- execute TP1 normally
- apply the configured TP1 protection normally

## 20. Strategy editing and locking

A selected account with an app-managed open trade locks the strategy from live-sensitive editing on that account.

While locked, the user cannot:

- change live strategy settings
- replace/deactivate the strategy for that account without first resolving the trade
- delete or rename the locked strategy

Clone remains allowed.

If the user switches to another account with no app-managed open trade, strategy editing is allowed there. Changes update the same saved strategy and apply to future evaluations; they do not rewrite the already-open broker trade on the inactive account.

This account-scoped lock is intentional: inactive accounts are not app-managed until selected again.

## 21. Confirmation policy

Sensitive actions always require confirmation.

This includes at least:

- Activate strategy
- Replace active strategy
- Deactivate strategy
- Delete strategy
- Reset strategy settings
- Enable/Go Live actions
- Switch away from an account with an open trade
- any action that changes live behavior while money is exposed

Normal non-destructive UI actions do not require confirmation.

## 22. Validation UX

Validation is structural/technical, not prescriptive trading advice.

Examples of required validation:

- strategy name present
- at least one symbol selected
- Trading Timeframe selected
- Trend Timeframe, if used, is higher than Trading Timeframe
- custom numeric fields present when Custom is selected
- Risk Method selected
- risk value > 0
- Stop Loss selected and valid
- TP2 selected and valid
- TP1 close % > 0 and <= 100 when TP1 enabled
- confirmation-close entry requires confirmation
- retest entry requires retest logic

The app must not reject a strategy solely because a risk percentage or R:R is unusual. Runtime broker constraints remain separate execution gates.

Errors are displayed directly under the invalid setting.

## 23. Strategy evaluator

The evaluator consumes the saved strategy definition and a sequence/current set of market facts.

Evaluation order:

1. Trend
2. BOS/CHOCH
3. Break Validation
4. Entry Confirmation
5. Entry Method
6. Stop Loss
7. TP1 / Protection
8. TP2
9. Risk sizing

Each step produces a simple state such as:

- Passed
- Waiting
- Failed / blocked
- Not applicable

The same evaluated state can power:

- Strategy Summary
- LIVE checklist/status
- Simulator Replay
- diagnostics

The strategy evaluator must not have separate LIVE and Simulator implementations.

## 24. Simulator

The Simulator is independent from LIVE.

A user may have Strategy A active in LIVE while simulating Strategy B or C.

Only saved strategies can be simulated. Unsaved builder state is not supported.

### 24.1 Inputs

The user chooses:

- saved strategy, active or inactive
- test symbol
- date range
- optional temporary risk override

For a strategy configured for Both, the user chooses EURUSD or XAUUSD for a run. A single-symbol strategy tests its configured symbol.

The starting balance is the current balance of the currently selected cTrader account.

If no risk override is provided, use the saved strategy risk. A simulator override never changes the saved strategy.

### 24.2 Simulation modes

#### Fast Run

Runs the complete chosen date range and returns final results.

#### Replay Mode

Supports:

- Play
- Pause
- Step forward
- playback speed
- chart annotations
- BOS/CHOCH
- validation/confirmation state
- Entry
- SL
- TP1
- TP2
- trade result

### 24.3 First-version execution model

Intentionally excluded from the first version:

- spread simulation
- commission simulation
- slippage simulation

The goal is to validate strategy logic first.

### 24.4 Results

Show at least:

- starting balance
- ending balance
- net P/L
- win rate
- total trades
- wins / losses
- average R
- max drawdown
- profit factor
- equity curve
- complete trade log

A trade-log row may jump Replay Mode directly to that trade for visual inspection.

### 24.5 Determinism requirement

Fast Run and Replay Mode must produce the same final set of trades for the same:

- strategy definition
- symbol
- date range / candle sequence
- starting balance
- risk override

LIVE and Simulator must share the evaluator. Given the same eligible closed-candle sequence and relevant account inputs, strategy qualification should be identical.

## 25. Persistence and state

Saved strategy definitions must be durable and survive backend restarts/deployments.

The schema-driven definition is the source of truth for user-created strategy configuration.

Runtime trade/order state remains separate from the strategy definition. Existing broker execution, account isolation, duplicate prevention, reconciliation, and cTrader safeguards are not replaced by Strategy Studio.

## 26. Error handling

Builder errors are field-specific and human-readable.

Examples:

- `Required — choose a TP2 method`
- `Enter a value greater than 0`
- `Trend timeframe must be higher than the 15m trading timeframe`

Runtime trade blocks must also be specific, for example:

- `Calculated volume is below broker minimum`
- `Stop distance is below broker minimum`

Do not silently mutate the user strategy to satisfy a broker rule.

## 27. Rollout plan

### Phase 1 — Strategy Studio CRUD/UI

Build:

- Strategy Studio page
- fixed builder sections
- validation
- live Strategy Summary
- save/edit/clone/delete
- activate/deactivate state representation

No change to current LIVE strategy behavior.

### Phase 2 — Simulator

Build the shared evaluator and Simulator Fast Run + Replay using saved strategy definitions.

LIVE still uses current production V3B.

### Phase 3 — V3B parity

Represent the current working V3B strategy as a Strategy Studio configuration.

Replay the same historical closed candles through:

- current V3B engine
- new shared Strategy Engine

Compare:

- BOS/CHOCH qualification
- break validation
- confirmation
- entry
- SL
- TP1/TP2
- protection
- risk sizing
- resulting trade decisions

Do not hand LIVE control to Strategy Studio until parity is demonstrated for the agreed V3B behavior.

### Phase 4 — LIVE handoff

After parity is verified, allow the active saved strategy to become the LIVE strategy source.

The existing broker execution safety system remains in place.

## 28. Test requirements

At minimum, tests must cover:

### Builder / persistence

- strategy survives restart
- blank/incomplete strategy cannot save
- conditional fields show only when relevant
- field-level validation
- Save updates same strategy
- Clone is an exact copy and starts inactive
- permanent Delete after confirmation
- one active strategy only
- live Strategy Summary matches configuration

### Strategy rules

- timeframe restrictions
- trend AND logic
- Break Validation AND logic
- Entry Confirmation AND logic
- BOS/CHOCH combined trigger behavior
- Last Swing SL uses Trading Timeframe
- TP1 optional behavior
- TP1 protection mandatory when TP1 enabled
- TP2 required
- Risk % uses cTrader balance
- Fixed $ risk sizing
- Both-symbol independent lot sizing

### Account behavior

- active strategy follows selected account
- only selected account receives new automated decisions/trades
- switch-away confirmation with open trade
- inactive account trade remains on cTrader without app management
- switch-back resumes management
- current-price TP1 check on reactivation
- no retroactive protection for TP1 missed while inactive
- later TP1 hit while active uses normal partial-close + protection behavior

### Simulator

- active and inactive saved strategies can simulate
- Simulator does not mutate LIVE strategy state
- starting balance comes from selected cTrader balance
- risk override is temporary
- Fast Run and Replay produce identical trades
- same evaluator is used by Simulator and future LIVE path

### Rollout safety

- current V3B remains untouched through Phase 1 and Phase 2
- parity tests compare old V3B vs new evaluator before LIVE handoff
- existing broker submission/idempotency/reconciliation/account-isolation safety tests remain green

## 29. Non-goals for the first version

The first version does not include:

- user-authored code or DSL
- AI-written strategy logic
- arbitrary node graphs
- Session Filter
- Extra Filters
- spread/commission/slippage backtesting
- automatic strategy version history
- strategy templates
- multiple simultaneously active LIVE strategies
- multiple simultaneously active LIVE accounts

## 30. Success criteria

Strategy Studio is ready for LIVE handoff only when:

1. users can build valid strategies entirely through fixed selections and numeric inputs
2. strategy definitions persist correctly
3. Simulator Fast Run and Replay agree
4. current V3B can be represented by the schema
5. the new evaluator reproduces the agreed current V3B behavior on parity data
6. account switching and broker safety remain unaffected
7. activating a saved strategy changes only strategy qualification; it does not bypass execution safeguards

Until these conditions are met, the existing V3B LIVE path remains authoritative.
