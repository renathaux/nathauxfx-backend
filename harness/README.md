# Isolated native AMD64 certification harness

The source/ directory is the independently pinned 369-file sanitized export.
Nothing in harness/ is part of its source manifest or a production build context.
No raw Git ancestry or runtime databases are published.

## Independent synthetic SQLite contract

legacy-schema.sql is hand-written fixed DDL from the historical initial migration
Backend/migrations/versions/20260807_0001_initial_schema.py at baseline
f839849ec9157da640c75b369379b91e135903d5. Its bytes are unchanged at composite
63b42795ca26bdecb1be6d7758c8ae757eee90d4. Historical migration SHA256:
38b186405690517f7742c832335ba9937b998e7482499282e3f39fe51d440f91.

DDL SHA256: 03489da97292902be00cb0c9cb2e6d97d43fd6df9b1b1668daa82d5054ab2a4e.
No models generate the DDL. No historical database/data are read, uploaded or
restored. Four empty tables preserve declared types, nullability, primary keys
and users indexes. INTEGER PRIMARY KEY gives SQLite rowid allocation.

The fixture proves only synthetic legacy source readability/schema registration,
not compatibility with historical data, trading, recovery or adoption authority.
Only two tests may see the file during their call phase. The plugin verifies the
source manifest before creation and after unconditional deletion. It never alters
the approved manifest. No fixture database is an artifact or build input.

Backend suites are the first certification gate. Production/maintenance builds
and container certification remain subsequent gates; backend success alone is
not a release certification or authorization to publish images.

## Pinned syntax-only input

harness/test-only-inputs/migrate_sqlite_to_neon.py is copied verbatim from the
same original path under Backend/scripts at composite
63b42795ca26bdecb1be6d7758c8ae757eee90d4, Git blob
cc969a9814e3ee3d6c7c8ebbc34df7eb567be7f4. Length 5788 bytes; SHA256
944d6cfd065d9f893344d0a28860813c694655706177f7b2c261de245f68480a.
The independent pin is in fixture.py, not read from the script itself.
The plugin materializes it exclusively during the database configuration
syntax test call, which only reads UTF-8 and ast.parse. No import or execution.
Unconditional deletion precedes exact source verification. No script bytes
enter release source, build context, images or evidence artifacts.
