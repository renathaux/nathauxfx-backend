# Native diagnostic broad baseline — specification and execution plan

## Authorization and release boundary

The user explicitly declares historical BLS backfill through the shipped CLI unsupported and outside THIS certified release surface. This is not permanent retirement. Image recipes contain the CLI but the default `bls_timestamp_manifest_12m.json` is absent. No support for that default workflow is claimed.

The frozen product remains `4c1c483086f08a8c2596cb400725071d03a1249e`, 376 entries, manifest `dbfa367c95c106c787b8338b282c2e83f4985cecb845ac3cb5bd8e233c32a0cc`, build `b6507d18c7c1c2ee542d2ccfee3ee385ba0ac812683672b9690bd66c383ad35a`. Product dependencies, tests and assertions are unchanged. Gold LIVE authority remains Strategy Studio/virtual Studio through the real FlowSignal pipeline, never legacy Gold fallback.

## Purpose and non-equivalence

Collect a NEW diagnostic reference candidate, not a passing release certificate. Preserve `broad-baseline.json` and run 38222628882 as historical evidence. Do not claim equivalence to either. A collected candidate cannot authorize backend/image jobs until its exact outcomes and classification receive independent review. Matching an approved failure baseline is not a claim that the whole repository is green.

## Native collection contract

- Two independent GitHub Ubuntu 24.04 x86_64 jobs, Python exactly 3.14.3, UID:GID 501:1000, existing denied-network namespace, no credentials, disposable PostgreSQL on local Unix socket only. No production services or image builds.
- Retain exact kernel, OS, Python implementation/build, SQLite library, Git/PostgreSQL versions, full application package profile and full test-tool package profile. Existing production hash lock remains unchanged. Pytest remains 8.4.2; record and compare its transitive installed dependencies across jobs (no silent equivalence if different).
- Child environment is an explicit allowlist derived from the existing isolated launcher: HOME=/nonexistent, PATH=/usr/bin:/bin, PYTHONDONTWRITEBYTECODE=1, PYTHON_DOTENV_DISABLED=1, PYTHONHASHSEED=0, TZ=UTC, LC_ALL=C.UTF-8, external test/harness PYTHONPATH and disposable-only VERIFIER_TEST_PG_DSN. DATABASE_URL and MIGRATION_DATABASE_URL are absent for the broad process. No credential-bearing environment dump.
- Use a disposable copy separate from frozen source/build context. Existing pinned broad Python overlay is unchanged. Add only pinned `Backend/INDICATOR_EVENT_STREAM.md` (Git frozen bytes, SHA256 `0d66543aad759a8d38d3470bfb422fda3d6e8494e880862fec3d0e6713762d12`, 5095 bytes) to that disposable copy. Do not add BLS JSON. No product JSON/DB is uploaded.
- Explicit NEW SQLite setup: create Backend/database directory and a fresh synthetic database by sqlite3 connect, PRAGMA user_version=0, commit and close. Require sqlite_master contains no user objects and integrity_check=ok before tests. No historical schema claims, no model-derived DDL, no fixture plugin, no production data. Retain exact setup SQL, SQLite version, initial schema and initial DB hash, not DB bytes. Record resulting post-suite schema/hash for diagnosis only.
- Local Git index is confined to the disposable copy, as in the existing broad harness. No .git in frozen source. Inventory exact test collection order before execution via read-only pytest hook; do not reorder tests.
- Run the complete broad suite without deselection/xfail/skip changes. The precise default-manifest test `tests.test_official_backfill.OfficialBackfillTests::test_verified_manifest_includes_new_bls_releases` is labelled OUTSIDE_RELEASE_SURFACE in the result, but still executed and its failure retained. No blanket exclusion of fundamentals tests. All other nonpassing nodes default to SUPPORTED_OR_UNEXPLAINED_PENDING_REVIEW until individually reviewed.
- Preserve full raw JUnit failure/error messages and bodies, all passing node identities, skips, stdout log, process return code, and collection order. Fingerprint uses canonical JSON of ordered-by-node records with status/full message/full body. NO address/UUID/path/dictionary-field normalization, NO first-line truncation. Different field/value/order/status outcomes remain differences. Sorting record order only provides deterministic indexing; actual collection order is separately retained.
- Diagnostic plugin only observes the two account-switch tests' complete synthetic EURUSD mirror and originating payload when available at failure; no application mutation. It records JSON-safe primitive values only and rejects unsupported types instead of using repr. No production values exist in these isolated jobs.
- Timestamp diagnostic: retain the cleanup test's outcome and any surviving synthetic result file names/mtimes from its traceback fixtures without modifying clocks, mtime, sort order or assertions. Existing controlled tie probe remains mechanism evidence only. Any inconsistent status between the two native jobs blocks a stable reference; do not retry until convenient or pick one outcome.
- Source verification runs before/after; disposable copy and DB are deleted; retained evidence is text only. Existing privileged identity/probe cleanup remains mandatory.

## Review and certification gate

1. Independently review this boundary/spec before native collection.
2. Add collection-only harness/tests and workflow; independently review implementation before push. No certification/build jobs in this collection workflow. Main untouched.
3. Collect two native runs. Compare complete failure records, pass/fail membership, collection order, environment/dependency/SQLite setup. Report exact differences without normalization.
4. Independently review collected candidate plus all supported-surface failures. No candidate automatically becomes reference. Unexplained semantic differences, inconsistent timing outcome or newly demonstrated supported product defect BLOCK.
5. Only if approved, pin a separate new baseline with provenance; retain historical baseline. Review comparator integration before full native rerun; unchanged required backend/image/reproducibility gates must all pass. No publication or Step 6.

## Harness plan

- [x] Spec/boundary independent review: approved for collection only; no reference approval.
- [x] Test-first exact fingerprint, single-node classification, empty SQLite setup and semantic diagnostic serialization: 7/7 local harness tests, including missing-observation and type-coercion regressions.
- [ ] Separate collector/plugin and collection-only workflow. Preserve existing `broad_gate.py` and historical baseline until new-reference review.
- [ ] Local tests, exact export check, independent patch review.
- [ ] Native collection; cleanup verification; independent result review.
- [ ] If approved, separately integrate pinned comparator and rerun full certification; otherwise report 5/6 BLOCKED.

No production access, LIVE change, broker access/write, Render mutation, epoch transition, hosted verifier, image publication or deployment is authorized.

Observer limitation: current/supplied dictionaries are compared only at failure; the fixture variable can alias the mirror, so this does not prove pre-sync immutability. Missing observations remain UNAVAILABLE. Recursive strict JSON validation rejects type coercion rather than replacing values with repr.
