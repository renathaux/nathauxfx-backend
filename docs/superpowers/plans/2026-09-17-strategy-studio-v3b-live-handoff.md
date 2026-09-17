# V3B Parity + Strategy Studio LIVE Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the shared evaluator matches the current V3B entry decision path, then add a separately gated Strategy Studio LIVE path that reuses the existing broker/idempotency safety system and supports the approved account-scoped TP1 management behavior.

**Architecture:** First run the new evaluator in read-only parity mode beside the frozen current V3B; no live behavior changes until parity evidence is green. For LIVE, add a parallel Strategy Studio setup lifecycle and extend the existing durable submission service to support that lifecycle without changing the current indicator-event/V3B behavior. A feature gate defaults OFF; only after explicit approval does the selected owner’s Studio active strategy become the candidate source for the selected cTrader account. Broker execution/risk/idempotency remain in the existing execution core. Post-entry TP1 management is Studio-specific and only operates on the currently selected account.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy/Alembic/PostgreSQL, existing cTrader execution core, existing TradeSubmissionAttempt reconciliation, shared strategy evaluator from Plan 2, pytest, static JavaScript/Node tests.

**Spec:** `docs/superpowers/specs/2026-09-16-strategy-studio-design.md`

## Global Constraints

- Plans 1 and 2 must be complete before this plan starts.
- Current V3B remains untouched and authoritative until the explicit LIVE handoff gate is enabled.
- No production gate may be enabled merely because tests pass; require explicit user approval after parity/shadow evidence.
- Only the currently selected cTrader account receives new Studio LIVE analysis/execution/management.
- Active Studio strategy follows the selected account.
- Switching away from an account with an open trade requires confirmation; after confirmation the position remains on cTrader and Studio app-side management stops for that inactive account.
- Returning to the account resumes management using the **currently active** Studio strategy and current broker state.
- On reactivation, if current price is already beyond TP1 and TP1 was not previously completed, do the partial close at current market price but do not retroactively apply the missed protection. If price is back below TP1, do nothing. A later TP1 hit while active uses normal partial-close + protection logic.
- If TP1 was already completed before switching away, it must never execute twice.
- TP2 stays broker-side whenever broker support allows it; SL/TP2 remain in force when the account is inactive.
- Broker account auth, one-position-per-symbol, risk checks, durable idempotency, reconciliation, and ambiguous-result fail-closed behavior remain mandatory.
- No account data/feed/lifecycle may cross account scope.
- The current frozen V3B has a legacy protection-only post-entry behavior that is not user-constructible under the approved Studio rule “TP1 enabled => partial close + protection; TP1 disabled => no protection.” Therefore parity in this plan must be exact for **signal qualification, entry, SL, TP2, side, and setup identity**, while current V3B post-entry management remains on its existing code path until a Studio strategy is explicitly activated. Do not silently auto-convert the current production V3B into a user Studio strategy with different management semantics.

---

## File Map

### Backend

- **Create `Backend/services/strategy_studio_parity.py`** — read-only V3B-vs-shared-evaluator comparison and mismatch report.
- **Create `Backend/services/strategy_studio_live_candidate.py`** — selected Studio strategy + shared evaluator → execution-shaped candidate; no broker call.
- **Create `Backend/services/strategy_studio_position_manager.py`** — selected-account TP1/protection management and suspend/resume semantics.
- **Create `Backend/services/strategy_studio_live_state.py`** — durable feature gate and account-management suspension state.
- **Modify `Backend/models.py`** — add `StrategySetupLifecycle` and `StrategyStudioLiveState`.
- **Create `Backend/migrations/versions/20260917_0024_strategy_studio_live.py`** — LIVE lifecycle/state tables plus `TradeSubmissionAttempt.lifecycle_kind` default.
- **Modify `Backend/services/trade_submission_service.py`** — preserve `claim_submission()` behavior and add strategy-lifecycle claim/transition support.
- **Modify `Backend/services/strategy_studio_service.py`** — account-scoped lock status once LIVE handoff is enabled.
- **Modify `Backend/routes/strategy_studio.py`** — parity/status/live-handoff endpoints.
- **Modify `Backend/routes/ctrader.py`** — invoke Studio suspend/resume hooks around a confirmed account switch only when Studio LIVE is enabled.
- **Modify the existing auto-evaluation integration point in `Backend/api.py`** — choose current V3B or Studio candidate behind the default-OFF gate; do not replace execution core.
- **Create `Backend/tests/test_strategy_studio_parity.py`**.
- **Create `Backend/tests/test_strategy_studio_submission.py`**.
- **Create `Backend/tests/test_strategy_studio_live_candidate.py`**.
- **Create `Backend/tests/test_strategy_studio_position_manager.py`**.
- **Create `Backend/tests/test_strategy_studio_live_handoff.py`**.

### Frontend

- **Modify `Frontend/strategy-studio.html`** — parity/live readiness banner and Go Live control.
- **Modify `Frontend/strategy-studio/strategy-studio.js`** — confirmation-gated live handoff and lock rendering.
- **Modify account-switch UI in `Frontend/app.html` / existing account controller** — open-trade switch warning from backend status.
- **Create `Frontend/tests/strategy_studio_live_handoff.test.js`** — confirmations/locked state/no silent switch.

---

### Task 1: Read-only V3B parity definition and replay comparator

**Files:**
- Create: `Backend/services/strategy_studio_parity.py`
- Create: `Backend/tests/test_strategy_studio_parity.py`

**Interfaces:**

```python
v3b_entry_parity_definition(symbol: str) -> dict
compare_v3b_entry_decisions(symbol: str, frame_5m: pd.DataFrame, *, account_scope: str) -> dict
```

Report:

```python
{
  "match": bool,
  "compared_setups": int,
  "legacy": [...],
  "studio": [...],
  "mismatches": [{"timestamp": ..., "field": ..., "legacy": ..., "studio": ...}],
  "parity_scope": ["side", "event_time", "confirmation_time", "entry", "sl", "tp2"],
  "post_entry_management_compared": False,
}
```

- [ ] **Step 1: Write failing parity tests**

Use fixed historical fixtures containing known V3B WAIT/BUY/SELL cases. Assert exact match for side, BOS/CHOCH event time, immediate-next-candle confirmation, entry, 5m swing SL, and 1.90R TP2.

Also assert:

```python
report = compare_v3b_entry_decisions(...)
assert report["post_entry_management_compared"] is False
assert "tp1_partial_close" not in report["parity_scope"]
```

- [ ] **Step 2: Run RED**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_studio_parity.py
```

- [ ] **Step 3: Build parity definition without changing the user schema**

The parity adapter uses the shared evaluator for entry rules:

```python
{
  "schema_version": 1,
  "symbols": [symbol],
  "trading_timeframe": "5m",
  "trend": {"timeframe": None, "methods": []},
  "structure": {
    "trigger": "BOS_CHOCH",
    "break_validation": ["CLOSE_BEYOND", "MIN_BODY_PERCENT"],
    "minimum_body_percent": 50.0,
    "minimum_distance_pips": None,
  },
  "confirmation": {
    "rules": ["NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND"],
    "minimum_body_percent": None,
  },
  "entry": {"method": "CONFIRMATION_CLOSE"},
  "stop_loss": {"method": "LAST_SWING", "buffer_pips": 0.0, "fixed_distance": None},
  "tp1": {"enabled": False, "target_r": None, "close_percent": None, "protection_r": None},
  "tp2": {"method": "FIXED_R", "value": 1.90},
  "risk": {"method": "PERCENT_BALANCE", "value": 1.0},
}
```

Where current V3B has symbol-specific SL buffer/minimum-distance behavior not representable in the user definition, the parity adapter supplies those existing frozen calculations as **comparison inputs**, not new user-facing settings. The shared evaluator must not silently change approved Studio schema to imitate legacy-only details.

- [ ] **Step 4: Compare against existing frozen V3B candidate builder**

Call the current V3B analysis path read-only; never lifecycle-update or execute. Normalize both outputs to the parity report fields before comparison.

- [ ] **Step 5: Run GREEN and commit**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_studio_parity.py
git add Backend/services/strategy_studio_parity.py Backend/tests/test_strategy_studio_parity.py
git commit -m "test: add Strategy Studio V3B parity harness"
```

---

### Task 2: Durable Strategy Studio setup lifecycle and shared submission protocol

**Files:**
- Modify: `Backend/models.py`
- Create: `Backend/migrations/versions/20260917_0024_strategy_studio_live.py`
- Modify: `Backend/services/trade_submission_service.py`
- Create: `Backend/tests/test_strategy_studio_submission.py`

**Interfaces:**

```python
claim_strategy_submission(setup_id, account_id, symbol, direction, payload,
                          *, owner_id, strategy_id, session_factory=None) -> dict
```

Existing `claim_submission(...)` remains byte-for-byte compatible at its public interface.

- [ ] **Step 1: Write failing atomic-claim tests**

Test two concurrent claims for the same Studio setup: exactly one succeeds. Test account A and account B with different setup IDs do not collide. Test existing indicator-event/V3B claims still pass unchanged.

- [ ] **Step 2: Add `StrategySetupLifecycle`**

```python
class StrategySetupLifecycle(Base):
    __tablename__ = "strategy_setup_lifecycle"
    setup_id = Column(String(96), primary_key=True)
    owner_id = Column(String(100), nullable=False, index=True)
    strategy_id = Column(String(64), nullable=False, index=True)
    account_id = Column(String(100), nullable=False, index=True)
    account_scope = Column(String(160), nullable=False)
    symbol = Column(String(20), nullable=False)
    direction = Column(String(8), nullable=False)
    status = Column(String(32), nullable=False)  # ELIGIBLE/SUBMITTING/CONSUMED/BLOCKED/RECONCILIATION_REQUIRED
    definition_snapshot = Column(JSON, nullable=False)
    initial_volume_units = Column(Integer)
    broker_position_id = Column(String(100))
    tp1_completed_at = Column(DateTime(timezone=True))
    protection_applied_at = Column(DateTime(timezone=True))
    management_suspended_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True), nullable=False)
```

- [ ] **Step 3: Add live state + migration**

```python
class StrategyStudioLiveState(Base):
    __tablename__ = "strategy_studio_live_state"
    owner_id = Column(String(100), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    enabled_strategy_id = Column(String(64))
    enabled_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True), nullable=False)
```

Migration `20260917_0024` has `down_revision = "20260917_0023"` and adds nullable/nonbreaking `lifecycle_kind` to `trade_submission_attempts` with server default `INDICATOR_EVENT`.

- [ ] **Step 4: Generalize attempt transitions without changing existing behavior**

Add private lifecycle adapter functions:

```python
def _load_lifecycle_for_attempt(session, attempt, *, for_update=False): ...
def _set_lifecycle_status(lifecycle, status, now): ...
```

When `attempt.lifecycle_kind == "STRATEGY_STUDIO"`, use `StrategySetupLifecycle`; otherwise use `IndicatorEventLifecycle`. Existing `claim_submission`, `complete_submission`, ambiguous reconciliation, and unsent recovery tests must remain green.

- [ ] **Step 5: Implement `claim_strategy_submission`**

Lock `StrategySetupLifecycle` by `setup_id`; require `ELIGIBLE`; transition to `SUBMITTING`; create the existing `TradeSubmissionAttempt` with:

```python
event_id=f"studio:{setup_id}"
signal_setup_id=setup_id
lifecycle_kind="STRATEGY_STUDIO"
```

Reuse the existing stable broker reference/idempotency functions.

- [ ] **Step 6: Run GREEN + PostgreSQL concurrency test and commit**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_strategy_studio_submission.py \
  tests/test_trade_submission_service.py \
  tests/test_trade_submission_postgres.py
git add Backend/models.py Backend/migrations/versions/20260917_0024_strategy_studio_live.py \
  Backend/services/trade_submission_service.py Backend/tests/test_strategy_studio_submission.py
git commit -m "feat: add durable Studio submission lifecycle"
```

---

### Task 3: Studio LIVE candidate in shadow mode

**Files:**
- Create: `Backend/services/strategy_studio_live_candidate.py`
- Create: `Backend/services/strategy_studio_live_state.py`
- Create: `Backend/tests/test_strategy_studio_live_candidate.py`

**Interfaces:**

```python
studio_live_enabled(owner_id, session_factory=None) -> bool
build_studio_candidate(owner_id, account_identity, symbol, market_bundle,
                       *, account_balance, prior_state, session_factory=None) -> dict
```

- [ ] **Step 1: Write failing candidate tests**

Required outcomes:
- gate OFF -> `WAIT_STUDIO_LIVE_DISABLED` and no lifecycle write;
- no Studio active strategy -> `WAIT_STUDIO_NO_ACTIVE_STRATEGY`;
- symbol not included -> `WAIT_STUDIO_SYMBOL_DISABLED`;
- evaluator WAIT -> no eligible lifecycle;
- evaluator READY -> deterministic setup ID + ELIGIBLE lifecycle, but still no broker call;
- same setup re-evaluation reuses lifecycle and cannot reopen CONSUMED.

- [ ] **Step 2: Define deterministic setup ID**

Hash only immutable setup identity:

```python
identity = {
    "owner_id": owner_id,
    "strategy_id": strategy_id,
    "schema_version": definition["schema_version"],
    "account_scope": account_identity.scope,
    "symbol": symbol,
    "direction": result.signal,
    "structure_event_time": result.structure_event_time,
    "entry_trigger_time": result.entry_time,
    "broken_level": result.broken_level,
}
setup_id = "sts1_" + sha256(canonical_json(identity)).hexdigest()[:48]
```

- [ ] **Step 3: Implement shadow candidate builder**

Candidate payload includes entry, SL, TP1, TP2, saved strategy ID, setup ID, account scope, evaluator steps, and `studio_live_ready`. It does not calculate broker lot size or call the broker; existing execution core remains responsible for account/risk/broker constraints.

- [ ] **Step 4: Run GREEN and commit**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_strategy_studio_live_candidate.py
git add Backend/services/strategy_studio_live_candidate.py Backend/services/strategy_studio_live_state.py \
  Backend/tests/test_strategy_studio_live_candidate.py
git commit -m "feat: add gated Studio LIVE candidate"
```

---

### Task 4: Parity/shadow production evidence gate

**Files:**
- Modify: `Backend/routes/strategy_studio.py`
- Modify: `Backend/tests/test_strategy_studio_parity.py`

**Interfaces:**

- `POST /strategy-studio/parity/run` — admin/owner only, read-only replay.
- `GET /strategy-studio/live-status` — exposes parity status and `enabled` flag.

- [ ] **Step 1: Add endpoint tests proving read-only behavior**

Assert parity endpoint never changes selection, LIVE Auto, event lifecycle, trade submissions, or cTrader positions.

- [ ] **Step 2: Add parity endpoint**

Response includes mismatch list and explicit `post_entry_management_compared: false`.

- [ ] **Step 3: Run historical parity over EURUSD and XAUUSD**

Use representative ranges including wins, losses, WAIT periods, BOS and CHOCH. Required gate before any live wiring:

```text
0 unexplained mismatches for side/event/confirmation/entry/SL/TP2 on accepted comparison setups.
```

Any legacy-only SL-buffer detail must be documented, not hidden.

- [ ] **Step 4: Commit evidence tooling**

```bash
git add Backend/routes/strategy_studio.py Backend/tests/test_strategy_studio_parity.py
git commit -m "feat: expose Strategy Studio parity diagnostics"
```

**CHECKPOINT:** Stop here and obtain explicit user approval before enabling or wiring Strategy Studio LIVE. Deploying parity/shadow code is allowed only with Studio LIVE gate OFF.

---

### Task 5: Wire Studio candidates to the existing execution core behind the default-OFF gate

**Files:**
- Modify: `Backend/api.py`
- Modify: `Backend/services/strategy_studio_live_candidate.py`
- Create: `Backend/tests/test_strategy_studio_live_handoff.py`

**Interfaces:** Candidate source switch only; broker execution remains `execute_live_order_core(..., source="auto")` and existing cTrader adapter.

- [ ] **Step 1: Write failing handoff tests**

Test matrix:
- gate OFF -> current V3B candidate/execution behavior unchanged;
- gate ON + Studio active -> current V3B is not also submitted;
- current selected account is pinned through evaluation/risk/submission;
- one-position-per-symbol still blocks;
- `claim_strategy_submission` happens before broker request;
- same setup cannot submit twice;
- broker ambiguous result stays reconciliation-required;
- account switch during submission cannot redirect the order.

- [ ] **Step 2: Add one candidate-source branch at the current auto execution boundary**

Pseudo-shape:

```python
if strategy_studio_live_enabled(owner_id):
    candidate = build_studio_candidate(...)
    submission_claim = claim_strategy_submission(...)
else:
    candidate = existing_v3b_candidate
    submission_claim = existing_indicator_event_claim
```

Do not fork broker placement/risk/reconciliation logic.

- [ ] **Step 3: Map Studio risk methods to existing risk sizing**

For `PERCENT_BALANCE`, use selected cTrader **balance** as required by spec. For `FIXED_DOLLARS`, convert to equivalent percent only at the execution adapter boundary:

```python
risk_percent = fixed_dollars / account_balance * 100.0
```

Then reuse existing symbol metadata/minimum/step sizing. Never silently alter requested risk when broker constraints block the order.

- [ ] **Step 4: Run execution safety suite GREEN**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_strategy_studio_live_handoff.py \
  tests/test_strategy_studio_submission.py \
  tests/test_ready_execution_handoff.py \
  tests/test_trade_submission_service.py \
  tests/test_simple_account_switch.py
```

- [ ] **Step 5: Commit while gate remains OFF**

```bash
git add Backend/api.py Backend/services/strategy_studio_live_candidate.py Backend/tests/test_strategy_studio_live_handoff.py
git commit -m "feat: wire gated Strategy Studio LIVE handoff"
```

---

### Task 6: Account-scoped Studio position management and suspend/resume

**Files:**
- Create: `Backend/services/strategy_studio_position_manager.py`
- Modify: `Backend/services/strategy_studio_service.py`
- Modify: `Backend/routes/ctrader.py`
- Create: `Backend/tests/test_strategy_studio_position_manager.py`

**Interfaces:**

```python
suspend_account_management(owner_id, account_identity, open_positions, *, session_factory=None) -> dict
resume_account_management(owner_id, account_identity, open_positions, prices, *, session_factory=None) -> dict
manage_selected_account_positions(owner_id, account_identity, open_positions, prices, *, session_factory=None) -> dict
```

- [ ] **Step 1: Write failing TP1/account-switch tests**

Cover:
1. switching away marks Studio lifecycles suspended and makes no broker mutation;
2. inactive account is never managed by background cycles;
3. switch back + TP1 not done + current price beyond TP1 -> partial close at current market, mark `tp1_completed_at`, do **not** move SL/protection on that reactivation action;
4. switch back + current price below TP1 -> no action;
5. later TP1 hit while continuously active -> partial close + configured protection;
6. `tp1_completed_at` prevents duplicate partial close across restart/switches;
7. TP1 disabled -> no partial-close manager action;
8. manual broker close removes/terminalizes lifecycle without opening another trade.

- [ ] **Step 2: Implement management only for current selected account**

Before every mutation, compare lifecycle `account_scope` with pinned current account scope. If mismatch, return `INACTIVE_ACCOUNT_NOT_MANAGED`.

Use existing broker partial-close/SL-modification functions; do not reimplement protocol. Persist lifecycle state only after broker-confirmed success; ambiguous broker mutation outcomes fail closed and require reconciliation.

- [ ] **Step 3: Implement strategy lock response**

When Studio LIVE is enabled and selected account has an open Studio-managed position, `strategy_studio_service` returns `locked: true` for the active strategy. Clone remains allowed. Rename/update/delete/deactivate reject while locked.

- [ ] **Step 4: Hook confirmed account switch**

`routes/ctrader.py` sequence when Studio LIVE enabled:

```text
read old selected account -> if open managed trade, require frontend confirmation -> suspend old management -> perform existing account switch -> resume/check positions on new selected account
```

Do not change the core account-switch identity/cache isolation implemented in existing code.

- [ ] **Step 5: Run GREEN and commit**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_strategy_studio_position_manager.py \
  tests/test_strategy_studio_live_handoff.py \
  tests/test_simple_account_switch.py
git add Backend/services/strategy_studio_position_manager.py Backend/services/strategy_studio_service.py \
  Backend/routes/ctrader.py Backend/tests/test_strategy_studio_position_manager.py
git commit -m "feat: manage Studio positions by selected account"
```

---

### Task 7: Frontend LIVE confirmation, locked state, and account-switch warning

**Files:**
- Modify: `Frontend/strategy-studio.html`
- Modify: `Frontend/strategy-studio/strategy-studio.js`
- Modify: existing account-switch UI in `Frontend/app.html` and its controller script.
- Create: `Frontend/tests/strategy_studio_live_handoff.test.js`

**Interfaces:** `GET /strategy-studio/live-status`; confirmation-gated `POST /strategy-studio/live-handoff` once backend endpoint is added in Task 8.

- [ ] **Step 1: Write failing UI tests**

Assert:
- Go Live requires confirmation;
- locked strategy disables edit/delete/deactivate but not Clone;
- switching away with an open Studio-managed trade shows exact warning that app management stops but cTrader trade/SL/TP stay open;
- cancel keeps current account;
- normal account switch without a managed open trade needs no extra warning.

- [ ] **Step 2: Render parity/readiness banner**

Before live gate approval show `Simulator ready — LIVE still uses current V3B`. After parity pass but gate off show `Parity verified — Go Live requires confirmation`.

- [ ] **Step 3: Add sensitive confirmations and locked controls**

No checkbox bypass; user must explicitly press Confirm in modal.

- [ ] **Step 4: Run GREEN and commit**

```bash
cd Frontend
node --test tests/strategy_studio_live_handoff.test.js tests/strategy_studio_*.test.js tests/account_switch_late_response.test.js
git add Frontend/strategy-studio.html Frontend/strategy-studio/strategy-studio.js Frontend/app.html \
  Frontend/tests/strategy_studio_live_handoff.test.js
git commit -m "feat: add Strategy Studio LIVE confirmations"
```

---

### Task 8: Explicit LIVE handoff endpoint and final safety gate

**Files:**
- Modify: `Backend/routes/strategy_studio.py`
- Modify: `Backend/services/strategy_studio_live_state.py`
- Modify: `Backend/tests/test_strategy_studio_live_handoff.py`

**Interfaces:**

`POST /strategy-studio/live-handoff`

```json
{"enabled": true, "confirm": true}
```

- [ ] **Step 1: Write failing authorization/gate tests**

Requirements:
- admin/owner authority only for enabling LIVE handoff;
- `confirm:true` mandatory;
- active saved strategy mandatory;
- latest parity record/status must be green for both configured symbols used by the strategy;
- cannot enable while existing Studio submission reconciliation is unresolved;
- disabling is also confirmation-gated;
- endpoint does not change `live_auto_trade_enabled`; that remains a separate user preference.

- [ ] **Step 2: Implement durable feature state**

Enabling writes only `StrategyStudioLiveState`. It does not itself submit an order. Runtime reads the state on evaluation cycles.

- [ ] **Step 3: Run full focused safety suite**

```bash
cd Backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_strategy_studio_parity.py \
  tests/test_strategy_studio_submission.py \
  tests/test_strategy_studio_live_candidate.py \
  tests/test_strategy_studio_position_manager.py \
  tests/test_strategy_studio_live_handoff.py \
  tests/test_trade_submission_service.py \
  tests/test_ready_execution_handoff.py \
  tests/test_simple_account_switch.py
```

Frontend:

```bash
cd Frontend
node --test tests/strategy_studio_*.test.js tests/strategy_studio_live_handoff.test.js \
  tests/account_switch_late_response.test.js
```

- [ ] **Step 4: Deployment sequence with gate OFF**

Deploy backend/frontend with `StrategyStudioLiveState.enabled = false`. Verify current production V3B continues normal WAIT/BUY/SELL behavior, current selected account is unchanged, LIVE Auto unchanged, and no Studio broker submission occurs.

- [ ] **Step 5: Obtain explicit user approval before first LIVE enable**

Do not enable in a migration, deploy hook, environment default, or test.

- [ ] **Step 6: First controlled LIVE-handoff verification after approval**

Use DEMO first. Verify:
- selected account exact;
- Studio active strategy exact;
- fresh account-scoped market data;
- no active position for target symbol unless expected;
- candidate evaluation and durable Studio lifecycle;
- one broker submission maximum for one naturally qualifying setup;
- broker SL/TP2 confirmed;
- TP1 manager state recorded;
- duplicate evaluation cannot resubmit.

Do not fabricate BOS/CHOCH eligibility for this final proof.

- [ ] **Step 7: Account-switch management proof**

With a controlled DEMO Studio-managed position, verify approved behavior: confirm switch away, old trade remains on cTrader unmanaged by app, new account becomes active; switch back and verify TP1 current-price logic and no duplicate TP1. If this cannot be tested safely with a natural position, keep the feature DEMO-only until evidence is available.

- [ ] **Step 8: Final completion criteria**

Only call the LIVE handoff complete when:
- current V3B remained unchanged with gate OFF;
- historical/shadow parity is green for entry path;
- generic Studio setup claim is atomic/idempotent;
- broker reconciliation remains shared and green;
- TP1 management follows selected-account suspend/resume rules;
- one Studio strategy active globally per owner;
- only selected cTrader account receives new orders;
- Simulator and LIVE consume the same evaluator;
- LIVE Auto remains a separate setting.
