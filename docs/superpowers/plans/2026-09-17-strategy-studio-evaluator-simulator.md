# Shared Strategy Evaluator + Simulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one deterministic strategy evaluator and use it to power Fast Run and candle-by-candle Replay simulation for any saved Strategy Studio strategy, without changing LIVE trading.

**Architecture:** Keep the evaluator broker-free and deterministic: it consumes a validated saved definition plus closed-candle market facts and returns step states and a trade plan. A simulator adapter loads the selected account’s durable account-scoped 5m candles, derives 15m/1h/4h frames deterministically, fetches the current selected cTrader balance read-only, and runs the same evaluator candle by candle. The frontend consumes simulator output for metrics, trade log, and replay chart controls. No simulator function may place/modify/close broker orders or change LIVE strategy state.

**Tech Stack:** Python 3, pandas, FastAPI, Pydantic v2, SQLAlchemy, existing SMC engine, pytest, static JavaScript/SVG, Node `node:test`.

**Spec:** `docs/superpowers/specs/2026-09-16-strategy-studio-design.md`

## Global Constraints

- Plan 1 CRUD/schema must already exist and its saved definition shape is authoritative.
- Simulator accepts saved strategies only; unsaved builder drafts are not supported.
- Simulator may run active or inactive saved strategies and never changes Studio activation or LIVE state.
- Starting balance is the current selected cTrader account **balance**, never equity.
- Risk override is temporary for one simulation and is never persisted.
- First version has no spread, commission, or slippage simulation.
- Fast Run and Replay must produce the same final trades/results for the same request.
- Shared evaluator is the only evaluator introduced here; do not create separate LIVE and Simulator rule engines.
- All strategy decisions use closed candles only. Synthetic/forming candles are not simulation inputs.
- Account-scoped histories must never be mixed. No copying candles between accounts.
- Existing production V3B remains the LIVE authority and is not modified in this plan.
- No broker order, partial close, SL/TP modification, or LIVE Auto mutation is allowed during implementation or simulator execution.

---

## File Map

### Backend

- **Create `Backend/services/strategy_engine/__init__.py`** — public evaluator exports.
- **Create `Backend/services/strategy_engine/types.py`** — evaluation/trade/setup state dataclasses.
- **Create `Backend/services/strategy_engine/market_facts.py`** — deterministic SMC/EMA/swing facts from closed frames.
- **Create `Backend/services/strategy_engine/evaluator.py`** — rule state machine: trend → structure → validations → confirmation → entry → SL/TP/risk budget.
- **Create `Backend/services/strategy_simulator_data_source.py`** — selected-account scoped durable history and timeframe aggregation.
- **Create `Backend/services/strategy_simulator.py`** — Fast Run/Replay execution, virtual balance, trade resolution, metrics.
- **Create `Backend/routes/strategy_simulator.py`** — authenticated simulator API.
- **Modify `Backend/api.py`** — include simulator router only.
- **Create `Backend/tests/test_strategy_engine_market_facts.py`**.
- **Create `Backend/tests/test_strategy_engine_evaluator.py`**.
- **Create `Backend/tests/test_strategy_simulator.py`**.
- **Create `Backend/tests/test_strategy_simulator_routes.py`**.

### Frontend

- **Create `Frontend/strategy-simulator.html`** — Simulator page shell.
- **Create `Frontend/strategy-simulator.css`** — mockup-aligned dark simulator layout.
- **Create `Frontend/strategy-simulator/strategy-simulator-api.js`** — simulation API wrapper.
- **Create `Frontend/strategy-simulator/strategy-simulator-model.js`** — local playback/filter/metric presentation state.
- **Create `Frontend/strategy-simulator/strategy-simulator-chart.js`** — dependency-free SVG candlestick/annotation renderer.
- **Create `Frontend/strategy-simulator/strategy-simulator.js`** — controller for Fast Run, Replay, Play/Pause/Step/speed/trade jump.
- **Modify `Frontend/strategy-studio/strategy-studio.js`** — enable Simulator action for saved strategies.
- **Modify `Frontend/app.html`** — optional direct Simulator menu entry.
- **Modify `Frontend/vercel.json`** — no-store simulator assets.
- **Create `Frontend/tests/strategy_simulator_model.test.js`**.
- **Create `Frontend/tests/strategy_simulator_page.test.js`**.
- **Create `Frontend/tests/strategy_simulator_chart.test.js`**.

---

### Task 1: Selected-account historical source and deterministic timeframe aggregation

**Files:**
- Create: `Backend/services/strategy_simulator_data_source.py`
- Create: `Backend/tests/test_strategy_engine_market_facts.py`

**Interfaces:**

```python
load_simulation_5m(symbol: str, start, end, *, stream_scope: str, session_factory=None) -> pd.DataFrame
aggregate_closed(frame_5m: pd.DataFrame, timeframe: str, *, end_exclusive) -> pd.DataFrame
load_market_bundle(symbol: str, start, end, *, stream_scope: str, session_factory=None) -> dict[str, pd.DataFrame]
```

Bundle keys: `5m`, `15m`, `1h`, `4h`.

- [ ] **Step 1: Write failing account-scope and aggregation tests**

```python
def test_load_uses_storage_symbol_for_exact_scope(session_factory):
    a = load_simulation_5m("EURUSD", START, END,
                           stream_scope="CTRADER:DEMO:47784297", session_factory=session_factory)
    b = load_simulation_5m("EURUSD", START, END,
                           stream_scope="CTRADER:DEMO:47810571", session_factory=session_factory)
    assert not a.equals(b)


def test_5m_to_15m_ohlc_is_deterministic():
    result = aggregate_closed(frame_5m, "15m", end_exclusive=pd.Timestamp("2026-09-17T01:30Z"))
    first = result.iloc[0]
    assert first.Open == frame_5m.iloc[0].Open
    assert first.High == frame_5m.iloc[:3].High.max()
    assert first.Low == frame_5m.iloc[:3].Low.min()
    assert first.Close == frame_5m.iloc[2].Close


def test_partial_final_bucket_is_excluded():
    result = aggregate_closed(frame_5m.iloc[:4], "15m", end_exclusive=pd.Timestamp("2026-09-17T00:20Z"))
    assert list(result.index) == [pd.Timestamp("2026-09-17T00:00Z")]
```

- [ ] **Step 2: Run RED**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_engine_market_facts.py -k "scope or aggregate or bucket"
```

Expected: module import failure.

- [ ] **Step 3: Implement exact account-scoped DB lookup**

Resolve the durable symbol using existing:

```python
from services.indicator_stream_account_scope import storage_symbol_for_scope
storage_symbol = storage_symbol_for_scope(symbol, stream_scope)
```

Query `IndicatorCandle` for `storage_symbol` + `timeframe == "5m"`, order ascending, and return only `[start, end)` rows. Required columns are `Open`, `High`, `Low`, `Close`, `Volume`. Raise `ValueError("SIMULATION_HISTORY_UNAVAILABLE")` when no rows exist.

- [ ] **Step 4: Implement deterministic resampling**

Use UTC left-anchored buckets:

```python
rule = {"15m": "15min", "1h": "1h", "4h": "4h"}[timeframe]
result = frame.resample(rule, label="left", closed="left", origin="epoch").agg({
    "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
}).dropna(subset=["Open", "High", "Low", "Close"])
```

Drop any bucket whose `bucket_start + timeframe_duration > end_exclusive`. Do not invent missing 5m candles; sparse cTrader history remains sparse.

- [ ] **Step 5: Run GREEN and commit**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_engine_market_facts.py -k "scope or aggregate or bucket"
git add Backend/services/strategy_simulator_data_source.py Backend/tests/test_strategy_engine_market_facts.py
git commit -m "feat: add account-scoped simulator history"
```

---

### Task 2: Market facts timeline from the existing SMC engine

**Files:**
- Create: `Backend/services/strategy_engine/types.py`
- Create: `Backend/services/strategy_engine/market_facts.py`
- Modify: `Backend/tests/test_strategy_engine_market_facts.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class CandleFacts:
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    body_percent: float

@dataclass(frozen=True)
class StructureEventFacts:
    timestamp: pd.Timestamp
    direction: str              # BUY or SELL
    event_type: str             # BOS or CHOCH
    broken_level: float
    invalidation_price: float | None

@dataclass(frozen=True)
class TrendFacts:
    bos_choch_direction: str | None
    ema50_direction: str | None
    ema200_direction: str | None
    swing_structure_direction: str | None

build_market_facts(bundle: dict[str, pd.DataFrame], symbol: str, trading_timeframe: str,
                   trend_timeframe: str | None) -> "MarketFactsTimeline"
```

- [ ] **Step 1: Write failing facts tests**

```python
def test_bos_and_choch_are_exposed_as_one_directional_trigger():
    timeline = build_market_facts(bundle_with_known_events, "EURUSD", "5m", None)
    event = timeline.structure_event(KNOWN_EVENT_TIME)
    assert event.direction in {"BUY", "SELL"}
    assert event.event_type in {"BOS", "CHOCH"}


def test_ema_direction_is_close_relative_to_ema():
    trend = timeline.trend(KNOWN_TIME)
    assert trend.ema50_direction == "BUY"


def test_swing_structure_requires_hh_hl_or_lh_ll():
    assert timeline.trend(BULL_TIME).swing_structure_direction == "BUY"
    assert timeline.trend(BEAR_TIME).swing_structure_direction == "SELL"
    assert timeline.trend(MIXED_TIME).swing_structure_direction is None
```

- [ ] **Step 2: Run RED**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_engine_market_facts.py
```

- [ ] **Step 3: Implement structure facts using existing analysis**

Use `indicators.smc.engine.analyze_structure(frame, timeframe=..., point_size=...)` on the full **closed** frame. This analyzer makes swings available at their `confirmed_index`; do not replace it with a new BOS/CHOCH implementation.

Map SMC event direction:

```python
"BULLISH" -> "BUY"
"BEARISH" -> "SELL"
```

Use `event_invalidation_swing.price` as candidate Last Swing SL reference.

- [ ] **Step 4: Implement EMA and swing-structure trend facts**

EMA:

```python
ema50 = frame.Close.ewm(span=50, adjust=False).mean()
ema200 = frame.Close.ewm(span=200, adjust=False).mean()
direction = "BUY" if close > ema else "SELL" if close < ema else None
```

Swing Structure uses the latest two **confirmed** highs and lows at each trend timestamp:
- latest high > previous high **and** latest low > previous low → BUY;
- latest high < previous high **and** latest low < previous low → SELL;
- otherwise `None`.

- [ ] **Step 5: Run GREEN and commit**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_engine_market_facts.py
git add Backend/services/strategy_engine/types.py Backend/services/strategy_engine/market_facts.py Backend/tests/test_strategy_engine_market_facts.py
git commit -m "feat: derive shared strategy market facts"
```

---

### Task 3: Deterministic shared strategy evaluator

**Files:**
- Create: `Backend/services/strategy_engine/evaluator.py`
- Create: `Backend/services/strategy_engine/__init__.py`
- Create: `Backend/tests/test_strategy_engine_evaluator.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class EvaluationState:
    status: str                 # WAITING, READY, BLOCKED
    pending_setup: dict | None

@dataclass(frozen=True)
class EvaluationResult:
    signal: str                 # WAIT, BUY, SELL
    steps: dict[str, dict]
    setup_id: str | None
    entry: float | None
    sl: float | None
    tp1: float | None
    tp2: float | None
    risk_budget: dict | None
    next_state: EvaluationState


evaluate_strategy(definition: dict, timeline, timestamp, prior_state: EvaluationState,
                  *, symbol: str, account_balance: float, risk_override: dict | None = None) -> EvaluationResult
```

- [ ] **Step 1: Write failing rule-order/AND tests**

Cover:

```python
# No BOS/CHOCH -> WAIT at structure step.
# Trend methods use AND: one disagreement -> WAIT/BLOCKED for this trigger.
# Break validations use AND.
# NEXT_SAME_DIRECTION evaluates the immediate next trading candle only.
# SECOND_CLOSE_BEYOND requires the confirmation close to remain beyond broken level.
# RETEST_LEVEL waits for touch + close back on setup side.
# confirmation body % checks the actual confirmation/retest candle.
# BOS_CHOCH_CLOSE enters on trigger close only when confirmation rules are empty.
# CONFIRMATION_CLOSE enters on qualifying confirmation close.
# RETEST enters on qualifying retest close.
```

Example:

```python
def test_all_selected_trend_filters_must_agree():
    definition = definition_with_trend(["BOS_CHOCH", "EMA_50"])
    result = evaluate_strategy(definition, disagreeing_timeline, T, empty_state(),
                               symbol="EURUSD", account_balance=10000)
    assert result.signal == "WAIT"
    assert result.steps["trend"]["state"] == "BLOCKED"
```

- [ ] **Step 2: Run RED**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_engine_evaluator.py
```

- [ ] **Step 3: Implement the exact evaluation order**

The `steps` keys and order are:

```python
["trend", "structure", "break_validation", "confirmation", "entry",
 "stop_loss", "tp1", "tp2", "risk"]
```

Step states are exactly `PASSED`, `WAITING`, `BLOCKED`, `NOT_APPLICABLE`.

A new BOS/CHOCH event creates pending setup state containing direction, event timestamp, broken level, invalidation swing, and trigger close. An opposite BOS/CHOCH invalidates an unfinished pending setup. `NEXT_SAME_DIRECTION`/`SECOND_CLOSE_BEYOND` fail if the immediate next trading candle does not qualify. A retest-only setup may remain pending until a retest qualifies or an opposite structure event invalidates it.

- [ ] **Step 4: Implement break math and entry methods**

Pip sizes:

```python
PIP_SIZE = {"EURUSD": 0.0001, "XAUUSD": 0.01}
```

Body percent:

```python
abs(close - open) / max(high - low, tiny) * 100.0
```

Minimum distance for BUY is `(close - broken_level) / pip_size`; SELL is `(broken_level - close) / pip_size`.

- [ ] **Step 5: Implement SL/TP/risk-budget plan generation**

Last Swing SL uses event invalidation price and optional buffer away from entry. Fixed distance uses definition value × symbol pip size.

TP2:
- FIXED_R: `entry ± abs(entry-sl) * value`;
- FIXED_DISTANCE: `entry ± value * pip_size`;
- OPPOSITE_SWING: nearest confirmed opposing swing in the profitable direction; no valid target → `WAITING` with reason `TP2_OPPOSITE_SWING_UNAVAILABLE`.

TP1 when enabled: `entry ± abs(entry-sl) * target_r`.

Risk budget is not lot size:

```python
if method == "PERCENT_BALANCE":
    dollars = account_balance * value / 100.0
else:
    dollars = value
```

A risk override replaces the method/value for this call only.

- [ ] **Step 6: Run evaluator tests GREEN and commit**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_engine_evaluator.py
git add Backend/services/strategy_engine Backend/tests/test_strategy_engine_evaluator.py
git commit -m "feat: add shared strategy evaluator"
```

---

### Task 4: Simulator trade resolution, TP1/protection, and metrics

**Files:**
- Create: `Backend/services/strategy_simulator.py`
- Create: `Backend/tests/test_strategy_simulator.py`

**Interfaces:**

```python
run_simulation(definition, market_bundle, symbol, start_balance, *, risk_override=None,
               include_replay=False) -> dict
```

Response contains `metrics`, `trades`, `equity_curve`, and optional `replay` snapshots.

- [ ] **Step 1: Write failing trade-resolution tests**

Cover:
- full SL = `-1R`;
- TP2 without TP1 = configured final R;
- TP1 partial close realizes `close_fraction * tp1_r` then remaining fraction continues;
- TP1 protection becomes active only after TP1 while simulation is active;
- Fast Run and Replay produce identical trades;
- risk % compounds from virtual balance after each closed trade;
- Fixed $ keeps dollar risk constant;
- risk override changes simulation only.

- [ ] **Step 2: Define deterministic intrabar policy in tests**

Because OHLC does not reveal path order:
- if pre-TP1 SL and TP1 are both touched in one candle, mark trade `AMBIGUOUS_INTRABAR` and exclude it from win/loss metrics rather than guessing;
- if TP1 and TP2 are touched on the profit side without SL touch, TP1 occurs first because price must cross TP1 to reach TP2;
- if TP1 and a newly armed protected SL could both be reached in the same candle, mark `AMBIGUOUS_INTRABAR` unless TP2 also resolves on a path that does not require an unknowable reversal.

Do not invent tick order.

- [ ] **Step 3: Run RED**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_simulator.py
```

- [ ] **Step 4: Implement virtual trade state**

Store per open virtual trade:

```python
{
  "entry": ..., "sl": ..., "tp1": ..., "tp2": ...,
  "side": "BUY" | "SELL",
  "risk_dollars": ...,
  "tp1_hit": False,
  "remaining_fraction": 1.0,
  "protected_sl": None,
  "realized_r": 0.0,
}
```

When TP1 hits, close the configured fraction and set protected SL to entry for breakeven or `entry ± risk_distance * protection_r` for positive custom R.

- [ ] **Step 5: Implement metrics**

Return:
- starting/ending balance;
- net P/L;
- win rate;
- total resolved trades, wins, losses;
- average R;
- max drawdown in dollars and percent;
- profit factor (`gross_profit / abs(gross_loss)`, `None` when no losses);
- equity curve after each trade;
- trade log with timestamps/levels/R/dollars/outcome.

- [ ] **Step 6: Run GREEN and commit**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_simulator.py
git add Backend/services/strategy_simulator.py Backend/tests/test_strategy_simulator.py
git commit -m "feat: add Strategy Studio simulator"
```

---

### Task 5: Authenticated Simulator API using saved strategies and current cTrader balance

**Files:**
- Create: `Backend/routes/strategy_simulator.py`
- Modify: `Backend/api.py`
- Create: `Backend/tests/test_strategy_simulator_routes.py`

**Interfaces:**

`POST /strategy-simulator/run`

Request:

```json
{
  "strategy_id": "strat_...",
  "symbol": "EURUSD",
  "start": "2026-08-01T00:00:00Z",
  "end": "2026-09-01T00:00:00Z",
  "mode": "FAST",
  "risk_override": null
}
```

`mode` is `FAST` or `REPLAY`.

- [ ] **Step 1: Write failing route tests**

Assert:
- unsaved/foreign-owner strategy IDs return 404;
- symbol must be allowed by saved strategy;
- current account snapshot must have a verified positive `balance`;
- route pins current cTrader account scope for the entire history load;
- simulation does not mutate strategy JSON/selection/LIVE Auto;
- `REPLAY` includes replay snapshots while `FAST` does not.

- [ ] **Step 2: Run RED**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_simulator_routes.py
```

- [ ] **Step 3: Implement request validation and current-balance read**

Use the same owner resolver as Strategy Studio. Capture selected account with existing `pinned_account()` before reading account snapshot/history. Read `balance` from `get_ctrader_account_snapshot()`; do not use `equity`.

Risk override shape:

```python
class RiskOverride(BaseModel):
    method: Literal["PERCENT_BALANCE", "FIXED_DOLLARS"]
    value: float
```

- [ ] **Step 4: Include simulator router and add no-execution source guard**

`Backend/api.py` only imports/includes the router. Source tests must reject references to broker order send/modify/close and LIVE Auto mutation from simulator modules.

- [ ] **Step 5: Run GREEN and commit**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_strategy_engine_market_facts.py \
  tests/test_strategy_engine_evaluator.py \
  tests/test_strategy_simulator.py \
  tests/test_strategy_simulator_routes.py
git add Backend/routes/strategy_simulator.py Backend/api.py Backend/tests/test_strategy_simulator_routes.py
git commit -m "feat: expose Strategy Studio simulator API"
```

---

### Task 6: Simulator frontend, Fast Run, and results

**Files:**
- Create: `Frontend/strategy-simulator.html`
- Create: `Frontend/strategy-simulator.css`
- Create: `Frontend/strategy-simulator/strategy-simulator-api.js`
- Create: `Frontend/strategy-simulator/strategy-simulator-model.js`
- Create: `Frontend/strategy-simulator/strategy-simulator.js`
- Modify: `Frontend/strategy-studio/strategy-studio.js`
- Create: `Frontend/tests/strategy_simulator_model.test.js`
- Create: `Frontend/tests/strategy_simulator_page.test.js`

**Interfaces:** Simulator state exposes selected strategy, symbol, date range, optional risk override, mode, result, active replay index.

- [ ] **Step 1: Write failing frontend tests**

Assert strategy selector loads saved active/inactive entries, user chooses symbol/date, starting balance is server-returned, risk override is temporary, and results render all required metrics.

- [ ] **Step 2: Run RED**

```bash
cd Frontend
node --test tests/strategy_simulator_model.test.js tests/strategy_simulator_page.test.js
```

- [ ] **Step 3: Build page and API client**

Top controls: Strategy, Symbol, Date Range, Risk Override, Fast Run, Replay. Results cards: Starting Balance, Ending Balance, Net P/L, Win Rate, Trades, Avg R, Max Drawdown, Profit Factor. Render equity curve as simple SVG polyline and trade log as accessible table.

- [ ] **Step 4: Enable Studio Simulator action**

Saved-strategy Simulator button navigates:

```javascript
window.location.href = `/strategy-simulator.html?strategy=${encodeURIComponent(strategyId)}`;
```

No simulator action changes active strategy.

- [ ] **Step 5: Run GREEN and commit**

```bash
cd Frontend
node --test tests/strategy_simulator_model.test.js tests/strategy_simulator_page.test.js
node --check strategy-simulator/strategy-simulator-api.js
node --check strategy-simulator/strategy-simulator-model.js
node --check strategy-simulator/strategy-simulator.js
git add Frontend/strategy-simulator* Frontend/strategy-studio/strategy-studio.js Frontend/tests/strategy_simulator_*.test.js
git commit -m "feat: add Strategy Studio simulator UI"
```

---

### Task 7: Replay chart and playback controls

**Files:**
- Create: `Frontend/strategy-simulator/strategy-simulator-chart.js`
- Modify: `Frontend/strategy-simulator/strategy-simulator.js`
- Create: `Frontend/tests/strategy_simulator_chart.test.js`

**Interfaces:**

```javascript
renderReplayChart(svgElement, candles, annotations, cursorIndex)
createPlaybackController({frames, onFrame, intervalMs})
```

- [ ] **Step 1: Write failing renderer/playback tests**

Test pure geometry helpers for candle x/y mapping and annotation labels; test Play/Pause/Step never advances past final frame and speed update changes interval.

- [ ] **Step 2: Run RED**

```bash
cd Frontend
node --test tests/strategy_simulator_chart.test.js
```

- [ ] **Step 3: Implement dependency-free SVG chart**

Render current replay window only. Draw wick/body, then annotations for `BOS`, `CHOCH`, `ENTRY`, `SL`, `TP1`, `TP2`. Do not add an external chart dependency in v1.

- [ ] **Step 4: Wire Play/Pause/Step/speed and trade-log jump**

Clicking a trade row finds the first replay frame at or before `entry_time`, sets cursor, pauses playback, and redraws. Replay does not make new backend calls after its run result is loaded.

- [ ] **Step 5: Run GREEN and commit**

```bash
cd Frontend
node --test tests/strategy_simulator_*.test.js
node --check strategy-simulator/strategy-simulator-chart.js
git add Frontend/strategy-simulator/strategy-simulator-chart.js Frontend/strategy-simulator/strategy-simulator.js Frontend/tests/strategy_simulator_chart.test.js
git commit -m "feat: add simulator replay playback"
```

---

### Task 8: Shared-engine/Simulator verification gate

**Files:** No new product files unless a verification defect is found.

- [ ] **Step 1: Prove Fast Run and Replay equivalence**

Run deterministic EURUSD and XAUUSD fixtures through both modes and assert identical trade IDs, entries, exits, R results, final balance, and metrics.

- [ ] **Step 2: Run backend regressions**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_strategy_studio_schema.py \
  tests/test_strategy_studio_service.py \
  tests/test_strategy_engine_market_facts.py \
  tests/test_strategy_engine_evaluator.py \
  tests/test_strategy_simulator.py \
  tests/test_strategy_simulator_routes.py \
  tests/test_simple_account_switch.py \
  tests/test_ready_execution_handoff.py
```

Expected: zero new failures.

- [ ] **Step 3: Run frontend regressions**

```bash
cd Frontend
node --test tests/strategy_studio_*.test.js tests/strategy_simulator_*.test.js tests/strategy_lab.test.js
```

- [ ] **Step 4: Verify no LIVE coupling**

Review diffs and source guards: current V3B runtime, `active_strategy_config_service`, trade submission, cTrader order send/modify/close, and LIVE Auto remain untouched.

- [ ] **Step 5: Release checkpoint**

Phase 2 may deploy only after review confirms simulation is analysis-only. Production acceptance is: saved strategies simulate with current selected-account balance/history; Fast Run and Replay agree; LIVE continues using current V3B exactly as before.
