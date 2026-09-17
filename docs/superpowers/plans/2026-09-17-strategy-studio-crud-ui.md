# Strategy Studio CRUD + UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the saved-strategy CRUD foundation and polished Strategy Studio UI without changing the production V3B execution path.

**Architecture:** Store each saved strategy as one validated JSON definition in Neon, scoped to the authenticated owner. Expose CRUD plus a Studio-only active selection through a dedicated FastAPI router, and build a standalone Strategy Studio page using the approved fixed selections, conditional fields, live summary, and confirmation dialogs. The Stage 1 active selection is metadata only and never influences PAPER/LIVE, cTrader, or LIVE Auto.

**Tech Stack:** Python 3, FastAPI, Pydantic v2, SQLAlchemy, Alembic, PostgreSQL/Neon, pytest, static HTML/CSS/JavaScript, Node `node:test`.

**Spec:** `docs/superpowers/specs/2026-09-16-strategy-studio-design.md`

## Global Constraints

- Fixed rule selections only; no arbitrary executable code, natural-language strategy generation, or node graph.
- User-created strategies start blank; no templates in Stage 1.
- Numeric fields may use custom typed values where the spec allows them.
- Conditional fields appear only when their parent option is relevant.
- One Studio active selection per owner; every Stage 1 API response exposes `live_handoff_enabled: false`.
- Stage 1 must not import or mutate `active_strategy_config_service`, LIVE/PAPER strategy state, broker positions, cTrader order paths, indicator lifecycle, or LIVE Auto.
- No Session Filter, Extra Filters, spread/news/volatility filters, or builder cooldown/max-trades controls.
- A strategy cannot be saved until all required structural validation passes; errors are field-specific.
- Sensitive UI actions require confirmation.
- Delete is permanent and has no archive/trash.
- Existing production V3B remains authoritative for LIVE throughout this plan.
- Use isolated databases for tests; do not mutate production Neon or place broker orders during implementation.

---

## File Map

### Backend

- **Create `Backend/services/strategy_studio_schema.py`** — canonical Pydantic definition, persisted enum vocabulary, cross-field validation, normalized summary.
- **Create `Backend/services/strategy_studio_service.py`** — owner-scoped CRUD, clone, permanent delete, Studio-only activation/deactivation.
- **Create `Backend/routes/strategy_studio.py`** — authenticated HTTP API.
- **Modify `Backend/models.py`** — add `SavedStrategy` and `StrategyStudioSelection`.
- **Create `Backend/migrations/versions/20260917_0023_strategy_studio.py`** — new durable tables.
- **Modify `Backend/api.py`** — include the new router only.
- **Create `Backend/tests/test_strategy_studio_schema.py`** — pure schema validation.
- **Create `Backend/tests/test_strategy_studio_service.py`** — CRUD/owner isolation/lifecycle.
- **Create `Backend/tests/test_strategy_studio_routes.py`** — auth/API/no-LIVE-side-effect tests.

### Frontend

- **Create `Frontend/strategy-studio.html`** — saved list + builder + summary shell.
- **Create `Frontend/strategy-studio.css`** — dark premium fintech styling based on the approved mockup direction.
- **Create `Frontend/strategy-studio/strategy-studio-model.js`** — pure blank model, visibility rules, client validation, summary.
- **Create `Frontend/strategy-studio/strategy-studio-api.js`** — API wrapper.
- **Create `Frontend/strategy-studio/strategy-studio.js`** — page controller and confirmation flows.
- **Modify `Frontend/app.html`** — Strategy Studio menu entry.
- **Modify `Frontend/apiClient.js`** — recognize `/strategy-studio/` owner-sensitive writes without breaking customer auth.
- **Modify `Frontend/vercel.json`** — no-store headers for the Studio page and scripts.
- **Create `Frontend/tests/strategy_studio_model.test.js`**.
- **Create `Frontend/tests/strategy_studio_page.test.js`**.
- **Create `Frontend/tests/strategy_studio_api.test.js`**.

---

### Task 1: Canonical strategy definition and validation

**Files:**
- Create: `Backend/services/strategy_studio_schema.py`
- Test: `Backend/tests/test_strategy_studio_schema.py`

**Interfaces:**
- Consumes: plain strategy definition dictionaries.
- Produces:
  - `StrategyDefinition.model_validate(payload) -> StrategyDefinition`
  - `normalize_definition(payload: dict) -> dict`
  - `validation_errors(payload: dict) -> dict[str, str]`
  - `strategy_summary(definition: dict) -> str`

- [ ] **Step 1: Write the failing schema tests**

```python
from services.strategy_studio_schema import normalize_definition, validation_errors


def valid_definition():
    return {
        "schema_version": 1,
        "symbols": ["EURUSD"],
        "trading_timeframe": "5m",
        "trend": {"timeframe": None, "methods": []},
        "structure": {
            "trigger": "BOS_CHOCH",
            "break_validation": ["CLOSE_BEYOND", "MIN_BODY_PERCENT"],
            "minimum_body_percent": 50,
            "minimum_distance_pips": None,
        },
        "confirmation": {
            "rules": ["NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND"],
            "minimum_body_percent": None,
        },
        "entry": {"method": "CONFIRMATION_CLOSE"},
        "stop_loss": {"method": "LAST_SWING", "buffer_pips": 5, "fixed_distance": None},
        "tp1": {"enabled": True, "target_r": 0.75, "close_percent": 80, "protection_r": 0.2},
        "tp2": {"method": "FIXED_R", "value": 2.0},
        "risk": {"method": "PERCENT_BALANCE", "value": 1.0},
    }


def test_valid_definition_round_trips():
    result = normalize_definition(valid_definition())
    assert result["symbols"] == ["EURUSD"]
    assert result["tp1"]["close_percent"] == 80.0


def test_trend_timeframe_must_be_higher():
    payload = valid_definition()
    payload["trading_timeframe"] = "15m"
    payload["trend"] = {"timeframe": "15m", "methods": ["EMA_50"]}
    assert validation_errors(payload)["trend.timeframe"] == "Trend timeframe must be higher than trading timeframe"


def test_confirmation_entry_requires_confirmation():
    payload = valid_definition()
    payload["confirmation"] = {"rules": [], "minimum_body_percent": None}
    assert "entry.method" in validation_errors(payload)


def test_retest_entry_requires_retest_confirmation():
    payload = valid_definition()
    payload["entry"]["method"] = "RETEST"
    assert "entry.method" in validation_errors(payload)


def test_tp1_enabled_requires_target_close_and_protection():
    payload = valid_definition()
    payload["tp1"]["protection_r"] = None
    assert "tp1.protection_r" in validation_errors(payload)
```

- [ ] **Step 2: Run the schema tests to verify RED**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_studio_schema.py
```

Expected: import failure because `services.strategy_studio_schema` does not exist.

- [ ] **Step 3: Implement the persisted vocabulary**

Use exactly:

```python
SYMBOLS = {"EURUSD", "XAUUSD"}
TRADING_TIMEFRAMES = {"5m", "15m", "1h"}
TREND_METHODS = {"BOS_CHOCH", "EMA_50", "EMA_200", "SWING_STRUCTURE"}
BREAK_RULES = {"CLOSE_BEYOND", "MIN_BODY_PERCENT", "MIN_DISTANCE"}
CONFIRMATION_RULES = {"NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND", "RETEST_LEVEL", "MIN_BODY_PERCENT"}
ENTRY_METHODS = {"BOS_CHOCH_CLOSE", "CONFIRMATION_CLOSE", "RETEST"}
STOP_METHODS = {"LAST_SWING", "FIXED_DISTANCE"}
TP2_METHODS = {"FIXED_R", "FIXED_DISTANCE", "OPPOSITE_SWING"}
RISK_METHODS = {"PERCENT_BALANCE", "FIXED_DOLLARS"}
TIMEFRAME_RANK = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
```

Use nested Pydantic v2 models with `ConfigDict(extra="forbid")`. Persist empty lists/`None`, never string sentinels like `"None"` or `"ALL"`. The UI expands `All` into the four real trend methods before save.

- [ ] **Step 4: Implement cross-field validation**

Rules:

```python
# Trend methods require a strictly higher trend timeframe.
# MIN_BODY_PERCENT requires the corresponding numeric percentage.
# MIN_DISTANCE requires a positive distance.
# CONFIRMATION_CLOSE requires at least one confirmation rule.
# RETEST entry requires RETEST_LEVEL.
# BOS_CHOCH_CLOSE cannot depend on future confirmation rules.
# LAST_SWING forbids fixed_distance; FIXED_DISTANCE requires it.
# OPPOSITE_SWING TP2 has value=None; FIXED_R/FIXED_DISTANCE require value > 0.
# Disabled TP1 normalizes target_r/close_percent/protection_r to None.
# Enabled TP1 requires all three values and 0 < close_percent <= 100.
# Risk value must be finite and > 0; no upper trading-style limit is imposed.
```

Return field paths such as `trend.timeframe`, `entry.method`, `tp1.close_percent` so the frontend can render errors directly under controls.

- [ ] **Step 5: Implement deterministic human-readable summary**

Example:

```text
5m BOS/CHOCH -> close beyond level + body >= 50% -> next candle same direction + second close beyond level -> confirmation close -> 5m swing SL + 5 pip buffer -> TP1 0.75R / close 80% / protect +0.2R -> TP2 2R -> risk 1% balance
```

- [ ] **Step 6: Run GREEN and commit**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_studio_schema.py
git add Backend/services/strategy_studio_schema.py Backend/tests/test_strategy_studio_schema.py
git commit -m "feat: define Strategy Studio schema"
```

---

### Task 2: Durable saved strategies and one Studio selection per owner

**Files:**
- Modify: `Backend/models.py`
- Create: `Backend/migrations/versions/20260917_0023_strategy_studio.py`
- Test: `Backend/tests/test_strategy_studio_service.py`

**Interfaces:**
- Produces SQLAlchemy models `SavedStrategy` and `StrategyStudioSelection`.

- [ ] **Step 1: Write failing model persistence tests**

```python
def test_saved_strategy_definition_is_durable(session_factory):
    row = SavedStrategy(
        strategy_id="strat-test-1", owner_id="user:abc", name="Breakout",
        schema_version=1, definition_json=valid_definition(), created_at=NOW, updated_at=NOW,
    )
    with session_factory() as session:
        session.add(row); session.commit()
    with session_factory() as session:
        assert session.get(SavedStrategy, "strat-test-1").definition_json["trading_timeframe"] == "5m"


def test_one_selection_per_owner(session_factory):
    with session_factory() as session:
        session.add(StrategyStudioSelection(
            owner_id="user:abc", strategy_id="strat-test-1", activated_at=NOW, updated_at=NOW,
        ))
        session.commit()
    with session_factory() as session:
        assert session.get(StrategyStudioSelection, "user:abc").strategy_id == "strat-test-1"
```

- [ ] **Step 2: Run RED**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_studio_service.py -k "durable or selection"
```

- [ ] **Step 3: Add the models**

```python
class SavedStrategy(Base):
    __tablename__ = "saved_strategies"
    strategy_id = Column(String(64), primary_key=True)
    owner_id = Column(String(100), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    schema_version = Column(Integer, nullable=False, default=1)
    definition_json = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_saved_strategy_owner_updated", "owner_id", "updated_at"),)


class StrategyStudioSelection(Base):
    __tablename__ = "strategy_studio_selection"
    owner_id = Column(String(100), primary_key=True)
    strategy_id = Column(String(64), ForeignKey("saved_strategies.strategy_id", ondelete="CASCADE"), nullable=False, index=True)
    activated_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
```

Do not enforce unique names; the spec did not request that restriction.

- [ ] **Step 4: Add Alembic revision**

```python
revision = "20260917_0023"
down_revision = "20260916_0022"
```

Create both tables/indexes. Downgrade must refuse when `saved_strategies` contains rows.

- [ ] **Step 5: Run GREEN and commit**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_studio_service.py -k "durable or selection"
git add Backend/models.py Backend/migrations/versions/20260917_0023_strategy_studio.py Backend/tests/test_strategy_studio_service.py
git commit -m "feat: persist Strategy Studio definitions"
```

---

### Task 3: Owner-scoped CRUD, clone, delete, and Studio-only activation

**Files:**
- Create: `Backend/services/strategy_studio_service.py`
- Modify: `Backend/tests/test_strategy_studio_service.py`

**Interfaces:**

```python
list_strategies(owner_id, session_factory=None) -> list[dict]
get_strategy(owner_id, strategy_id, session_factory=None) -> dict
create_strategy(owner_id, name, definition, session_factory=None) -> dict
update_strategy(owner_id, strategy_id, name, definition, session_factory=None) -> dict
clone_strategy(owner_id, strategy_id, clone_name, session_factory=None) -> dict
delete_strategy(owner_id, strategy_id, confirmed, session_factory=None) -> bool
activate_strategy(owner_id, strategy_id, confirmed, session_factory=None) -> dict
deactivate_strategy(owner_id, strategy_id, confirmed, session_factory=None) -> dict
```

Every returned strategy includes `state`, `summary`, `locked: false`, and `live_handoff_enabled: false` in Stage 1.

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_owner_cannot_read_another_owners_strategy(sessions):
    created = create_strategy("user:a", "A", valid_definition(), sessions)
    with pytest.raises(StrategyStudioNotFound):
        get_strategy("user:b", created["strategy_id"], sessions)


def test_clone_is_exact_inactive_copy(sessions):
    source = create_strategy("user:a", "Original", valid_definition(), sessions)
    activate_strategy("user:a", source["strategy_id"], True, sessions)
    clone = clone_strategy("user:a", source["strategy_id"], "Copy", sessions)
    assert clone["definition"] == source["definition"]
    assert clone["state"] == "INACTIVE"


def test_activation_replaces_only_studio_selection(sessions):
    first = create_strategy("user:a", "A", valid_definition(), sessions)
    second = create_strategy("user:a", "B", valid_definition(), sessions)
    activate_strategy("user:a", first["strategy_id"], True, sessions)
    current = activate_strategy("user:a", second["strategy_id"], True, sessions)
    assert current["live_handoff_enabled"] is False
    assert get_strategy("user:a", first["strategy_id"], sessions)["state"] == "INACTIVE"


def test_active_strategy_must_be_deactivated_before_delete(sessions):
    created = create_strategy("user:a", "A", valid_definition(), sessions)
    activate_strategy("user:a", created["strategy_id"], True, sessions)
    with pytest.raises(StrategyStudioConflict, match="deactivate"):
        delete_strategy("user:a", created["strategy_id"], True, sessions)
```

- [ ] **Step 2: Run RED**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_studio_service.py
```

- [ ] **Step 3: Implement CRUD transactions**

Generate IDs as `strat_` + `uuid.uuid4().hex`. Normalize every definition before write. Query every mutation using both `strategy_id` and `owner_id`; never mutate a row fetched without owner scope. Use `SELECT ... FOR UPDATE` on the selection row during activate/deactivate.

Activation/deactivation must never call V3B, auto-trade, cTrader, broker execution, lifecycle, or `active_strategy_config_service` functions.

- [ ] **Step 4: Implement exact clone and permanent delete**

Use `copy.deepcopy(source.definition_json)`. Clone starts inactive. Delete requires `confirmed is True` and rejects the current Studio selection until deactivated.

- [ ] **Step 5: Run GREEN and commit**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_studio_service.py
git add Backend/services/strategy_studio_service.py Backend/tests/test_strategy_studio_service.py
git commit -m "feat: add Strategy Studio CRUD service"
```

---

### Task 4: Authenticated Strategy Studio API

**Files:**
- Create: `Backend/routes/strategy_studio.py`
- Modify: `Backend/api.py`
- Create: `Backend/tests/test_strategy_studio_routes.py`

**Interfaces:**

- `GET /strategy-studio/strategies`
- `POST /strategy-studio/validate`
- `POST /strategy-studio/strategies`
- `GET /strategy-studio/strategies/{strategy_id}`
- `PUT /strategy-studio/strategies/{strategy_id}`
- `POST /strategy-studio/strategies/{strategy_id}/clone`
- `POST /strategy-studio/strategies/{strategy_id}/activate`
- `POST /strategy-studio/strategies/{strategy_id}/deactivate`
- `DELETE /strategy-studio/strategies/{strategy_id}` with `{"confirm": true}`

- [ ] **Step 1: Write failing auth/API tests**

```python
def test_unauthenticated_list_is_rejected(client):
    assert client.get("/strategy-studio/strategies").status_code in {401, 403}


def test_validate_returns_field_errors_without_saving(client, customer_headers):
    response = client.post("/strategy-studio/validate", headers=customer_headers,
                           json={"name": "", "definition": {}})
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["errors"]


def test_create_never_enables_live_handoff(client, customer_headers):
    response = client.post("/strategy-studio/strategies", headers=customer_headers,
                           json={"name": "My Strategy", "definition": valid_definition()})
    assert response.status_code == 201
    assert response.json()["strategy"]["live_handoff_enabled"] is False
```

Also assert user B cannot read user A's ID.

- [ ] **Step 2: Run RED**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_studio_routes.py
```

- [ ] **Step 3: Implement owner resolution**

Reads accept `current_user(request)`; customer writes use `current_user_with_csrf(request)`. Legacy owner/admin fallback follows `routes/admin_access.py` using `_bearer` and `api.SESSIONS`.

```python
def owner_key(actor):
    if hasattr(actor, "id"):
        return f"user:{actor.id}"
    email = str(actor.get("email") or "legacy-admin").strip().lower()
    return f"owner:{email}"
```

Do not use cTrader account ID as strategy ownership.

- [ ] **Step 4: Implement request models and `/validate`**

```python
class StrategyWriteRequest(BaseModel):
    name: str
    definition: dict

class StrategyCloneRequest(BaseModel):
    name: str

class ConfirmRequest(BaseModel):
    confirm: bool = False
```

`/validate` never writes and returns `valid`, `errors`, `normalized_definition`, and `summary`.

- [ ] **Step 5: Include router in `Backend/api.py` only**

```python
from routes.strategy_studio import router as strategy_studio_router
app.include_router(strategy_studio_router)
```

- [ ] **Step 6: Add no-LIVE import assertions, run GREEN, commit**

Assert Studio backend source does not reference `place_market_order`, `claim_submission`, `live_auto_trade_enabled`, or `save_active_strategy_settings`.

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_strategy_studio_schema.py \
  tests/test_strategy_studio_service.py \
  tests/test_strategy_studio_routes.py
git add Backend/routes/strategy_studio.py Backend/api.py Backend/tests/test_strategy_studio_routes.py
git commit -m "feat: expose Strategy Studio API"
```

---

### Task 5: Pure frontend builder model and live summary

**Files:**
- Create: `Frontend/strategy-studio/strategy-studio-model.js`
- Create: `Frontend/tests/strategy_studio_model.test.js`

**Interfaces:**

```javascript
blankStrategy()
visibleFields(definition)
normalizeForApi(definition)
clientValidation(definition)
buildSummary(definition)
trendTimeframeOptions(tradingTimeframe)
```

- [ ] **Step 1: Write failing Node tests**

```javascript
const test = require('node:test');
const assert = require('node:assert/strict');
const StudioModel = require('../strategy-studio/strategy-studio-model.js');

test('trend timeframe options are strictly higher', () => {
  assert.deepEqual(StudioModel.trendTimeframeOptions('5m'), ['15m', '1h', '4h']);
  assert.deepEqual(StudioModel.trendTimeframeOptions('15m'), ['1h', '4h']);
  assert.deepEqual(StudioModel.trendTimeframeOptions('1h'), ['4h']);
});

test('conditional fields follow methods', () => {
  const value = StudioModel.blankStrategy();
  value.stop_loss.method = 'LAST_SWING';
  value.tp1.enabled = true;
  const visible = StudioModel.visibleFields(value);
  assert.equal(visible.stopBuffer, true);
  assert.equal(visible.fixedStopDistance, false);
  assert.equal(visible.tp1, true);
});
```

- [ ] **Step 2: Run RED**

```bash
cd Frontend
node --test tests/strategy_studio_model.test.js
```

- [ ] **Step 3: Implement the pure model**

Use exactly the backend keys. UI `All` expands before save to:

```javascript
['BOS_CHOCH', 'EMA_50', 'EMA_200', 'SWING_STRUCTURE']
```

Client validation is for immediate UX only; backend remains authoritative.

- [ ] **Step 4: Implement readable summary, run GREEN, commit**

```javascript
return [trend, `${tf} BOS/CHOCH`, breakText, confirmation, entry, stop, tp1, tp2, risk]
  .filter(Boolean).join(' → ');
```

```bash
cd Frontend
node --test tests/strategy_studio_model.test.js
node --check strategy-studio/strategy-studio-model.js
git add Frontend/strategy-studio/strategy-studio-model.js Frontend/tests/strategy_studio_model.test.js
git commit -m "feat: add Strategy Studio client model"
```

---

### Task 6: Strategy Studio API client and polished three-column page

**Files:**
- Create: `Frontend/strategy-studio.html`
- Create: `Frontend/strategy-studio.css`
- Create: `Frontend/strategy-studio/strategy-studio-api.js`
- Create: `Frontend/strategy-studio/strategy-studio.js`
- Create: `Frontend/tests/strategy_studio_api.test.js`
- Create: `Frontend/tests/strategy_studio_page.test.js`

**Interfaces:**

```javascript
listStrategies()
validateStrategy(name, definition)
createStrategy(name, definition)
updateStrategy(id, name, definition)
cloneStrategy(id, name)
activateStrategy(id)
deactivateStrategy(id)
deleteStrategy(id)
```

- [ ] **Step 1: Write failing page/API tests**

Assert the page contains `Saved Strategies`, `Strategy Builder`, `Strategy Summary`, `Save Strategy`, `Clone`, `Activate`, `Delete`; assert it does **not** contain `Session Filter` or `Extra Filters`. Assert API source references `/strategy-studio/strategies` and `/strategy-studio/validate` and never calls `execute-live-order`, `place_market_order`, or `live-auto-toggle`.

- [ ] **Step 2: Run RED**

```bash
cd Frontend
node --test tests/strategy_studio_api.test.js tests/strategy_studio_page.test.js
```

- [ ] **Step 3: Implement API wrapper with both auth modes**

Customer requests send:

```javascript
const token = String(sessionStorage.getItem('flowsignal_user_session_token') || '').trim();
const csrf = String(sessionStorage.getItem('flowsignal_csrf_token') || '').trim();
if (token) headers.Authorization = `FlowSignalUser ${token}`;
if (csrf) headers['X-CSRF-Token'] = csrf;
```

Owner-tab bearer injection remains handled by existing `apiClient.js`.

- [ ] **Step 4: Build the approved page structure**

```html
<aside id="savedStrategiesPanel"></aside>
<main id="strategyBuilder"></main>
<aside id="strategySummaryPanel"></aside>
```

Builder sections are exactly: name/symbols, trading timeframe, optional trend, structure/break validation, confirmation, entry, stop, TP1/protection, TP2, risk.

- [ ] **Step 5: Implement conditional rendering and inline errors**

Every field change updates local draft, visible controls, client errors, and summary. Save first calls backend `/validate`; backend field errors render directly under matching controls. Save remains disabled until valid.

- [ ] **Step 6: Implement one reusable sensitive-action confirmation modal**

Activation copy must explicitly say Stage 1 does not change LIVE. Delete copy says permanent/no restore. Simulator button is visible but disabled with text `Simulator becomes available after the shared evaluator is installed.`

- [ ] **Step 7: Style to the approved mockup direction**

Use dark navy/charcoal panels, gold/blue accents, compact saved cards, grouped center controls, sticky summary, and responsive single-column fallback below 1000px. Do not add fake performance or market statistics.

- [ ] **Step 8: Run GREEN and commit**

```bash
cd Frontend
node --test tests/strategy_studio_model.test.js tests/strategy_studio_api.test.js tests/strategy_studio_page.test.js
node --check strategy-studio/strategy-studio-model.js
node --check strategy-studio/strategy-studio-api.js
node --check strategy-studio/strategy-studio.js
git add Frontend/strategy-studio.html Frontend/strategy-studio.css Frontend/strategy-studio/ Frontend/tests/strategy_studio_*.test.js
git commit -m "feat: build Strategy Studio interface"
```

---

### Task 7: Navigation, auth routing, and no-store delivery

**Files:**
- Modify: `Frontend/app.html`
- Modify: `Frontend/apiClient.js`
- Modify: `Frontend/vercel.json`
- Modify: `Frontend/tests/strategy_studio_page.test.js`
- Modify: `Frontend/tests/strategy_studio_api.test.js`

**Interfaces:** App menu navigates to `/strategy-studio.html`; owner mutation detection includes Strategy Studio writes.

- [ ] **Step 1: Add failing navigation/auth assertions**

```javascript
assert.match(appHtml, /menuStrategyStudioBtn/);
assert.match(appHtml, /Strategy Studio/);
assert.match(apiClient, /\/strategy-studio\//);
```

- [ ] **Step 2: Run RED**

```bash
cd Frontend
node --test tests/strategy_studio_page.test.js tests/strategy_studio_api.test.js
```

- [ ] **Step 3: Add menu button and route**

Add beside Live Trading:

```html
<button id="menuStrategyStudioBtn" class="menu-row" title="Strategy Studio">
  <span class="menu-row-icon">◇</span><span class="menu-row-text">Strategy Studio</span>
</button>
```

Click navigates to `/strategy-studio.html`.

- [ ] **Step 4: Extend owner mutation detection and no-store headers**

Only write methods under `/strategy-studio/` are owner-sensitive. Add `Cache-Control: no-store, max-age=0` for the Studio HTML and its three scripts.

- [ ] **Step 5: Run regressions and commit**

```bash
cd Frontend
node --test tests/strategy_studio_*.test.js tests/strategy_lab.test.js
node --check apiClient.js
git add Frontend/app.html Frontend/apiClient.js Frontend/vercel.json Frontend/tests/strategy_studio_*.test.js
git commit -m "feat: link Strategy Studio from app"
```

---

### Task 8: Stage 1 integration verification and release gate

**Files:** No new product files unless a verification defect is found.

**Interfaces:** Confirms Stage 1 has zero execution coupling.

- [ ] **Step 1: Run focused backend suite**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_strategy_studio_schema.py \
  tests/test_strategy_studio_service.py \
  tests/test_strategy_studio_routes.py \
  tests/test_simple_account_switch.py \
  tests/test_ready_execution_handoff.py \
  tests/test_trade_submission_service.py
```

Expected: zero new failures.

- [ ] **Step 2: Run focused frontend suite**

```bash
cd Frontend
node --test tests/strategy_studio_*.test.js tests/strategy_lab.test.js
```

Expected: pass.

- [ ] **Step 3: Verify migration on an isolated database**

```bash
cd Backend
alembic upgrade head
alembic current
```

Expected head contains `20260917_0023`. Do not run this against production during implementation.

- [ ] **Step 4: Verify no LIVE code path changed**

Review `git diff main...HEAD`. Acceptance requires no new call from Strategy Studio to broker order/modify/close, V3B runtime, event lifecycle, or LIVE Auto state.

- [ ] **Step 5: Exercise the Stage 1 lifecycle in an isolated app**

Create → edit → clone → activate clone → deactivate clone → delete clone → restart → verify original persists. Every response still contains `live_handoff_enabled: false`.

- [ ] **Step 6: Release checkpoint**

Backend/frontend PRs may be created only after independent review. Stage 1 production acceptance is CRUD/UI persistence with current V3B, selected cTrader account, open broker positions, and LIVE Auto unchanged. Do **not** enable a saved strategy for LIVE in this plan.
