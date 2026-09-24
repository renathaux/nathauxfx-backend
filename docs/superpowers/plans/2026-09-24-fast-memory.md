# FAST worker memory reduction

User requirement: profile, reduce FAST subprocess peak to <=120 MiB preferred / <=150 MiB maximum; preserve exact saved Gold831, Gold832 configuration and representative EURUSD results; cold5Y <=30s preferred / <=45s maximum. No deployment, LIVE changes or saved strategy edits.

Frozen before source: commit7502626 at ../memory-reference/Backend. Production94d80e2 is unchanged, observed baseline317.5MiB with512MiB limit.

1. Measure before: stage RSS and object counts for history/raw/month frames, each aggregate, structure/swings/trendfacts, evaluation/diagnostics/trades/equity/serialization/cache.
2. Independently reduce history/aggregation transients and retained fact representations. Keep numeric float64, legacy initialization boundaries and availability timing. Only required timeframes.
3. Integrate lazy chronological window consumption, default-off fullfactcache, single-worker queue, release stage references, bounded result output.
4. Read saved strategy settings through signed-in UI, freeze equivalent normalized definitions without saving. Benchmark before7502626 and after on identical canonical history; filteredGold832 is an offline variant only.
5. Exact trade/metric/equity/diagnostic and fact-level parity; tests for boundary state and no future data, cache/concurrency/cleanup.
6. Measure fresh subprocess cold RSS/runtime and3 sequential fulljobs with parent/child RSS and cleanup. Include remote raw-cache path. Compare worst-case observed memory against productionheadroom; do not infer productionSAFE from cache-only or incomplete measurements.
7. Commit local backend changes and report all measurements and limitations. No deploy/push.

Completed verification (2026-09-24):
- Preserved frozen7502626 results and stage measurements before optimizing.
- Isolated worker startup avoids LIVE/database bootstrap imports, without changing that bootstrap.
- Numeric history/required-TF aggregation, compact arrays and one-window-at-a-time evaluation replace full-history objects. Fact caching disabled; raw cache disk-only forFAST.
- Exact5Y parity: Gold831 savedsettings, identicalsaved832settings, offlinefiltered832,EURUSD,andopposite-swingfixture. Savedstrategies unchanged.
-178 targeted regressiontests pass. Independent review:119focusedtests pass,noactionablefindings.
- Fulltests collection blocked by existing missingservices.deriv_user_connection_store;confirmed absent in7502626too.
- Threeactualsubprocessjobs releaseallworkerRAM; parentRSSsettleswithin11.3MiB ofbaseline,run2→3 increasesonly16KiB.
- Local measured workerbudget fitsmaximum150MiB; combinedRender/Linuxcgrouppeak remainsunmeasured. NoSAFEproductionclaim.
- Allchanges remainlocal; nopush/deploy/LIVEchanges/orders.
