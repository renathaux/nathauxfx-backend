# Studio LIVE Authority Implementation Plan

> For agentic workers: use executing-plans for routing/integration and focused parallel workers for independent management/frontend tasks.

Goal: explicit symbol entry authority, durable snapshot-bound Studio management, truthful live conditions and protection UI.
Architecture: one read-only authority resolver binds each symbol to the enabled saved strategy, NONE otherwise; legacy V3B needs explicit per-symbol opt-in and cannot override Studio. Existing execution gates remain; broker management uses durable lifecycle snapshots independent of current selection. Existing live display map is the compatibility envelope for authority/evaluation and position management.
Tech stack: Python/FastAPI/SQLAlchemy/Postgres/Alembic and browser JS.
Spec: /Users/nathauxfx/.codex/attachments/7109abe6-a14e-4941-b207-6d766cb6030d/Pasted text.txt

## Constraints
No manual broker changes, LIVE Auto changes, strategy settings changes, or forced production trades. Production audits read-only. Backend deploy healthy before frontend merge/deploy. Saved Gold definition is source of truth.

## Review focus
- Runtime installer overrides bypass selector: test installed wrapper, not only selector.
- News candidate replacement cannot acquire authority; test BUY replacement when NONE/Studio WAIT.
- Request crash before response: durable TP1 claim blocks resubmission after restart.
- Broker request ok is not broker SL confirmation: readback must match or improve target.
- Missing display metadata cannot pretend V3B authority; explicit NONE must render block.

## Tasks
- [ ] Routing: add services/execution_authority.py resolve_execution_authority(symbol, profile, owner_id, account_scope, legacy_symbols); API loads current profile, fail closed on errors/mismatch. Test Gold-only XAU and EUR, WAIT, no LIVE strategy, explicit legacy. Patch live_v3b_runtime_install.py routing and pre-submission authority fence; prevent news replacement.
- [ ] Snapshot/management: models.py additive JSON fields + migration; trade_submission_service.py immutable claim snapshot; position manager durable request-before-send, reconciliation and broker SL readback; tests E-L and concurrency/restart. Export managed_position_states mapping.
- [ ] Presentation: API live_strategy_display_by_symbol attaches authority from same resolver, explicit NONE block, dynamic saved rules. Mirror durable management state only for proven positions. Frontend consumes authority, dynamic conditions and actual management; tests M/N and no false confirmed protection.
- [ ] Integration: run baseline/targeted suites, update stale expected legacy defaults only when semantic change intentional; independent review of complete diff and address findings.
- [ ] Deploy: PR/backend merge, Render migration/deploy verify; frontend merge and Vercel verify; browser/read-only logs XAU Studio, EUR NONE. Record exact SHAs/deploy IDs/production definition hash and trade evidence.

## Ledger
Initial inspection: production Gold v18 XAUUSD-only enabled, selection matches. strategy_setup_lifecycle empty. Installed V3B runtime loop bypasses Studio selector entirely when global V3B flag on. Recent EUR SELL accepted 2026-09-28T13:10:45.883Z, broker position59771722/order172554304, INDICATOR_EVENT source; exact event/setup IDs recorded in audit.
Ruling: user supplied complete design, tests and explicit full implementation/deployment authorization; proceed without redundant design approval. Independent management and frontend code ownership delegated per parallel-investigation skill; parent owns API/routing/integration/deploy.
