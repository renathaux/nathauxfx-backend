# Administrative handoff freshness recertification

Source: 8b7c3d4bae1ae973a5bc9b58d4471fd47345f279, 388 files.
Manifest: a7828ecc7a3445b01bb4b4ca36e9dc9648569e759faa9e865b59d41c78a7273f.
Build identity: 5e63edfe32fc0f204790b6bdd26ce076124bb705435660f2ff3aaa5b30f898b0.
Parent: 8824e32da6a5d11427dea40d4236eb38dfe4d829.

Only the seven reviewed product paths changed. Frozen dependencies, production
startup, strategies, risk, adoption/reconciliation semantics and numerical
20-second freshness contract remain unchanged. Administrative observations now
overlap independent DB and broker reads; two observations still remain required.

The prior broad diagnostic reference is preserved. Exactly 41 new tests are
added to its previous 49-node additive matrix. All historical cases, order and
semantic fingerprints remain required. New tests must pass; no normalization
of failures or automatic baseline blessing is permitted. Historical BLS CLI
backfill remains outside this release's supported surface, despite shipping
the CLI without its default historical manifest.

Native suites add 25 concurrent-reader tests, 16 existing-module regressions,
and six calibrated full-path trials. Timing inputs contain only measured
durations, never production payloads or credentials. Synthetic fixtures exceed
the measured 3,403,219-byte snapshot, retain six streams/37 lineage visits,
use two complete broker protocol observations and actual disposable PG locks.
The first ownership DML is rejected before execution; measured DB/FS state must
remain unchanged. Setup/teardown is confined to disposable fixtures.

The six timing trials include three measured component profiles and three
component-max sensitivity profiles. Their worst pre-denied-write total plus
the independently reviewed 1.421998-second variance/extra-RTT reserve must be
strictly below 20 seconds. This is a bounded maintenance-attempt acceptance
gate, not a statistical tail guarantee. NOWAIT contention fails closed.

Each independent production/maintenance build must preserve exact source,
dependency and protected-path identity. No registry publication or production
operation occurs in this certification workflow. All image publication later
must transfer these already-tested OCI bytes, without rebuilding.
# Harness environment correction after run 38744375513

The broad comparison correctly rejected ENVIRONMENT_DRIFT: pytest's unpinned
transitive test-tool dependency resolved pluggy 1.7.0 rather than the retained
reference's 1.6.0. Comparing recorded environments under the unchanged comparator
showed no other non-path difference. Pin pluggy 1.6.0 in backend and image test-tool
installation. This changes neither product dependencies nor the frozen source,
and does not change or normalize the baseline. Retain the failed run and rerun
the complete native certification.
