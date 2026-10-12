# Auth bridge coverage-input correction

Scope: repair the 159/170 collection gap from native run 38274242993. No product behavior or assertion changes. Count remains 170; helper tests are not additionally selected.

## Provenance

Both files exactly match tracked bytes in reviewed composite/application commit `4c1c483086f08a8c2596cb400725071d03a1249e` and the pre-existing independently pinned `harness/broad-inputs.json` materials:

| Original path | Bytes | SHA256 | Git blob |
|---|---:|---|---|
| Backend/tests/test_strategy_studio_owner_boundary.py | 8312 | 8ed98937e21329470c29c230bcdf8b23ae2dda495b1261257c07c92a981438d8 | 32c16f76a43ae0947c8eb6565071570254b5ecc5 |
| Backend/tests/test_strategy_studio_service.py | 7964 | 0960a3adda2314e2af4d951c537cf35dc9882645df6ac60ed8c0b237935e15a9 | 6b8b76649f46dcd8e69552abd822518e84989a81 |

Owner-boundary imports the helper's `session_factory` and `valid_definition`. The former creates an in-memory SQLite engine using StaticPool and the existing models; the latter supplies a synthetic strategy definition. No historical DB or production data is involved. Other imports resolve to standard library, pinned test dependencies, or unchanged frozen product modules (`db`, routes.strategy_studio, services.strategy_studio_owner/models/service/live_state). Pytest supplies monkeypatch/caplog/parametrize. Neither file imports another test helper or conftest. Neither is imported by production runtime.

## Isolation

The exact two files are written to a unique sibling `auth-test-overlay-*` directory, never inside source/. The auth-bridge subprocess alone receives the absolute owner-boundary test path. Pytest's normal test-module path handling makes its sibling helper importable. Helper test functions are not selected. Eleven owner-boundary cases and zero helper cases are checked in JUnit, as well as the unchanged 170/0/0/0 total.

The directory is removed by a context manager, including exception paths. Source verification runs before/after. The frozen release remains 376 entries with manifest `dbfa367c95c106c787b8338b282c2e83f4985cecb845ac3cb5bd8e233c32a0cc`.

Image jobs are separate fresh runners. Existing packaging consumes only verified source/, rejects extra files, and never copies harness/broad-inputs or the ephemeral sibling directory. Production/maintenance recipes and image gates are unchanged. No overlay file enters release source, manifest, product context or OCI content.

Required glob expansion now fails explicitly if a requested pattern has no file, instead of silently removing coverage. No test assertion was edited, no expected count lowered, and no new product freeze created.
