# Immutable stream generation recovery

Local implementation only. No production rollout or broker action is authorized by this document.

## Model and deployment ordering

`indicator_stream_generations` identifies `(root_key, timeframe, generation)` and maps it to a unique storage key. G1 keeps the original key, event IDs and all historical contents. `indicator_stream_heads` selects the active generation. A partial unique index permits only one ACTIVE registry entry per root/timeframe. Head resolution fails closed on missing/inconsistent registry entries. The existing `indicator_stream_state.status` of G1 is deliberately not changed.

G2 uses a separate deterministic storage key. Existing event hashing includes that key; confirmation IDs include the source event ID; setup/idempotency/client IDs inherit the event namespace. Studio uses a separate additive binding table and sorted generation bindings in setup hashes. No legacy ID is rewritten.

Alembic revision `20260925_0026` adds metadata and assigns existing streams G1. It does not update legacy tables. Newly initialized runtime streams register G1 in their creation transaction. Downgrade is refused once G2 or Studio generation bindings exist. Runtime code requires the migrated schema. Old application processes do not implement generation fences: they must not remain serving when an eventual cutover is authorized. Do not roll back to pre-generation application code after creating G2; keep LIVE Auto off and fix forward instead.

The admin dry-run can inspect a legacy schema without creating tables. Its report says `MIGRATION_REQUIRED`; such a plan cannot authorize apply. An eventual separately approved schema migration must be followed by a fresh dry-run against the migrated schema.

## Authoritative input contract

The recovery process is broker-free. Acquire a CLOSED cTrader history export separately through an approved read-only market-data path. Do not import the connector into this command. No market data was fetched from production for local verification.

JSON shape:

```json
{
  "source": "ctrader_closed_history",
  "scope": "CTRADER:DEMO:48817926",
  "symbol": "XAUUSD",
  "timeframe": "5m",
  "fetched_at": "2026-09-22T04:10:00Z",
  "candles": [
    {"timestamp": "2026-09-22T04:00:00Z", "Open": 4342, "High": 4343, "Low": 4335, "Close": 4336.81},
    {"timestamp": "2026-09-22T04:05:00Z", "Open": 4336.80, "High": 4338.62, "Low": 4335.96, "Close": 4336.71}
  ]
}
```

This abbreviated example is not a usable recovery export. Real input must start exactly at the predecessor's persisted origin and cover its complete watermark through the latest closed bar at export time. Maximum 25,000 candles bounds this administrative operation; larger history is blocked, never truncated. Inputs must have finite, valid OHLC, aligned timestamps, no duplicates, no forming bars, matching durable selected account and configuration. Each absent bar must fall within a recognized closure (New York DST-aware weekend and gold maintenance; exact documented EURUSD rollover omission). Unknown gaps/holidays block. Source provenance remains an operator responsibility: JSON labels are not a cryptographic proof of broker origin.

## Local command examples

Run from the repository root, with an explicitly selected local database. Never rely on a developer `.env` for verification.

```sh
DATABASE_URL="$LOCAL_DATABASE_URL" PYTHONPATH=Backend python Backend/scripts/stream_generation_recovery.py \
  --history closed-history.json \
  --plan-file generation-plan.json \
  --snapshot-file generation-1-snapshot.json
```

Default is dry-run; `--dry-run` is also supported. It reports account/scope/stream, generations/status/watermarks, irreversible events/attempts, full coverage/gaps/duplicates, new row counts, cutoff, snapshot/history/replay hashes, schema status, LIVE Auto status and broker capability. It writes plan/snapshot files with mode 0600 and refuses to overwrite existing files. A SAFE dry-run is an assessment, not permission to apply.

Explicit local apply:

```sh
DATABASE_URL="$LOCAL_DATABASE_URL" PYTHONPATH=Backend python Backend/scripts/stream_generation_recovery.py \
  --apply --history closed-history.json \
  --plan-file generation-plan.json \
  --snapshot-file generation-1-snapshot.json
```

Apply verifies the recorded snapshot, requires a successful current plan with migrated schema, locks active account and LIVE setting, requires LIVE Auto OFF, blocks unresolved/in-flight submissions, and repeats all history/replay/snapshot checks. Missing/unknown LIVE state blocks. Changing the account also blocks idempotent retries. A stale plan must be regenerated with new artifact filenames.

## Cutover and continuation

One transaction acquires the root-symbol generation lock, snapshots G1, creates G2 candles/state/events, verifies stored full-replay equivalence, marks G1 registry FROZEN, creates G2 ACTIVE and switches the head. Only generation metadata changes for G1. Any failure rolls back. PostgreSQL uses a transaction advisory lock shared by writers/claims/admin; SQLite tests use a writer lock. Retries recognize the committed generation/history/snapshot and create nothing twice.

All bootstrap events are historical. Activation is the final bootstrap candle open timestamp, following the existing stream convention. Both a future event and its durable CLOSED 5m confirmation must be strictly later. Confirmation source, symbol, side, broken level, timestamp, close and hash are checked; submission must match the eligible lifecycle's confirmation. Old G1 and historical G2 claims are refused. Startup resolves the durable head; no old-generation fallback is permitted. Charts prefer canonical active bars and derive current structure from the complete canonical prefix. `stream_generations.audit(session, root_key, timeframe, generation)` is the explicit historical reader.

Studio discards prior evaluator state on advanced generations and uses canonical input for every advanced timeframe. Missing canonical timeframe blocks. A setup's binding set must match all advanced heads, each bound generation must still be current/READY, and its future confirmation must exist durably in 5m. Sorted bindings make setup identity independent of database row order.

## Verification boundary

The local synthetic incident reproduces unchanged structure, changed consumed confirmation close, and accepted broker evidence. It is not the production 14-event candle export. Local replay/bootstrap/continuation parity and row preservation pass. PostgreSQL advisory-lock and migration behavior have been inspected but not exercised against a PostgreSQL server in this task. Before eventual production apply, obtain an authorized fresh export and inspect the production dry-run; a local PASS does not authorize deployment, schema migration, cutover, broker orders or enabling LIVE Auto.
