# Observability successor certification boundary

Release source: 8824e32da6a5d11427dea40d4236eb38dfe4d829, 383 source files.
Manifest: dbe202f54b38e854b7873bda45557ec4b7263498f9c457b5215caa3fb276fb50.
Deterministic source archive: c8a76c9d77ff6dd15a499f1ec85434d939947f4e357c9af0424f5917649c8876.
Immutable build identity: f4fef835fdd30b5c4060e1e0dab7fe32e4491400f1db8c363bbed0fafe1671c8.

The reviewed predecessor remains 4c1c483086f08a8c2596cb400725071d03a1249e.
Only the reviewed twelve product/documentation paths differ. All other prior
export bytes, dependencies, base-image pin and maintenance packaging are unchanged.
The new export adds two runtime modules, two test modules and three documentation
files. Historical runtime JSON, SQLite, credentials and generated state remain excluded.

## Diagnostic reference

Retain every original native-reference file and hash, raw failure message/body,
full semantic observations, explicit SQLite setup and environment comparison.
Do not claim whole-repository green or equivalence to unproven older history.

The sole collection extension is the explicit 49 nodes in observation_cases.py.
The old collection order is preserved exactly, with those two new modules inserted
in alphabetical module order. Parameter ordering is not sorted or normalized.
Every one of those 49 nodes must execute and pass in broad.xml; missing, duplicate,
failed, errored or skipped new cases block. The separate mandatory runtime-identity
suite also requires 49/49 with zero failures/errors/skips.
The full raw record fingerprint is retained and independently matched to broad.xml.
Exactly the 49 new records must equal complete canonical passed records (empty
failure body/message). Only after that proof are those exact records separated;
the entire old record set is compared by the unchanged comparator. No new failure,
skip, error, duplicate or changed old result can be filtered away.

The previous test-only broad overlay retains its pinned predecessor provenance.
Its complete byte set is checked against the new Git source before publication.
The approved auth-only overlay remains test-only and is removed before builds.

Historical BLS backfill via the shipped CLI remains unsupported and outside this
release's certified operational surface. Its default JSON is intentionally absent.
This is not project-wide retirement and no global test/assertion is deleted.

## Gates and safety

All seventeen prior required backend suites remain mandatory, plus the new suite.
Production/maintenance container gates, disposable migration/no-op checks,
two independent AMD64 builds, reproducibility and independent OCI download
verification remain required. Permissions are contents:read only.

No production or broker credentials, production access, image publication,
deployment, recovery handoff or LIVE enablement. Option B is a reviewed plan only.
