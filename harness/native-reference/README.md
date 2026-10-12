# New native diagnostic reference — not historical equivalence

Reference run: 38270359938, first of two independent AMD64 collections. Both yielded 3375 passed, 48 failed, 1 skipped, 0 errors, with identical membership, order and pinned package/runtime/SQLite setup. Five remaining logging cases passed in fresh processes in run 38273220486. Old broad-baseline.json and broad_gate.py remain historical and unchanged.

This reference is regression comparability only, not whole-repository success. All mandatory backend suites, container tests, migration dry runs and independent image reproducibility gates remain required and unchanged.

## Release-specific boundary

Historical BLS backfill via the shipped CLI is unsupported and outside this certified release surface. Recipes ship the CLI but omit its default bls_timestamp_manifest_12m.json. This is not permanent project retirement. The single corresponding test remains run and its exact failure remains recorded. No other fundamental test is excluded by this decision.

## Limitations reviewed independently

Missing disposable SQLite tables prevent some chart/V3B/account-switch/snapshot tests reaching their behavioral assertions. Direct-call FastAPI Query defaults, Backend/Backend script paths and stale legacy settings/payload assertions are retained as diagnostic failures, not repaired product behavior. Seven logging assertions are suite-context dependent, with fresh-process passes; exact disabling call not proved. One PostgreSQL test requires a separate localhost TCP variable unavailable in this network-denied harness; it stays recorded as skipped, not coverage. Former cleanup failure now passes; historical cause is not established. Full synthetic dictionary observations match across native jobs, but truncated historical representations do not prove historical equality.

## Exact comparison contract

Five raw JSON reference files are pinned in native_reference_gate.py. Full records, status membership, collection order, SQLite schema/hash and environment/packages are checked. No first-line-only comparison. Reference raw canonical fingerprint is 3b597f3a32de8b5245a0bb174dafe15e6a967bb7be7c717f84c170816b1581d3.

Entire message/body fields are anchored to reference literals. Only existing bound temporary-root slots in seven explicit nodes and pytest LogCaptureFixture address slots in five explicit nodes can vary. The stream-admin import node alone also has a literal `...tmp/` source-root slot in pytest's truncated CompletedProcess representation; its full underlying error remains matched. Slot counts and surrounding text are fixed by the pinned reference. Unknown nodes, extra slots, suffix, value, dictionary, UUID, status, error or message changes block. Both forward and reverse reconstruction are required and replacement bindings retained.

Environment substitutions are limited to structurally validated disposable cwd, interpreter, evidence, PYTHONPATH and local PostgreSQL socket fields. Every other field including versions is exact. No secret/production DSN is used. Raw evidence is never overwritten by the comparison view.

No reference result authorizes Step 6, production access, LIVE, publication or deployment. Future Gold LIVE execution must remain through certified Strategy Studio / virtual Studio and FlowSignal's real pipeline.
