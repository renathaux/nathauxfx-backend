# Git-free runtime identity adapter

`live_integrity.build_identity.capture()` has one release mode. It never runs
Git, reads identity from environment variables, contacts a network service,
imports broker execution, or writes/regenerates evidence. No development Git
fallback remains. Missing evidence fails with `BUILD_IDENTITY_UNVERIFIED`.

## Independent trust anchor

The future packaging process must independently provision
`/opt/flowsignal-release/expectations.json`, outside the application source
manifest. It contains exactly the immutable fields below. It must come from
reviewed release provenance, not from copying an untrusted runtime build record.
No request parameter, environment override or record-selected path can change
this location. The package root is derived from the installed adapter location.

The adapter compares the build record against these independent expectations,
then verifies the manifest bytes, every manifested source file, the dependency
lock and dependency metadata. It also checks the actual Python patch version.
Self-consistent edits to source + manifest + record do not replace the pin.

Trust ultimately rests on the future immutable image/release installation:
both application and anchor must be root-owned, inaccessible for replacement
by the non-root runtime UID, including their ancestor directories. No writable
overlay, ACL, mount or plugin/import path may override them. The adapter checks
read-only file modes and evidence-directory modes, rejects symlink components
and hard-linked evidence, and detects changes during reads. POSIX mode checks
alone do not prove mount immutability or protection against a privileged attacker.
The image stage must prove those installation properties; this task does not
create a container, signature, trust anchor, release record or final OCI digest.

## Build record v1

`<package-root>/build-record.json` is a JSON object with these required string
fields (also the exact expectations schema):

- `schema`: `flowsignal-immutable-build/v1`
- `packaging_source_sha`: lowercase 40-digit Git object identity
- `source_manifest_sha256`: lowercase 64-digit SHA-256
- `dependency_lock_sha256`: lowercase 64-digit SHA-256
- `dependency_metadata_sha256`: lowercase 64-digit SHA-256
- `python_version`: exact `major.minor.patch`
- `base_image_digest`: `sha256:` followed by 64 lowercase hex digits
- `certified_dependency_run_id`: positive decimal run ID string
- `certified_dependency_commit`: lowercase 40-digit Git object identity

Only the build record may additionally contain `build_timestamp` (a string).
It is informational, not returned as authority and not hashed. Unknown fields,
duplicate JSON keys, non-finite constants, missing fields and unknown versions
fail closed. The base digest and certification provenance are checked against
the release pin, not independently attested by querying an OCI registry/GitHub.
The adapter does not claim to inspect the running image digest.

## Deterministic identity

Select exactly the nine required fields. Encode with Python JSON
`sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False`,
then UTF-8, no BOM and no trailing newline. The SHA-256 hex digest of those
bytes is `build_identity`. Lexicographic key order, record whitespace/key order
and informational timestamps cannot change the result. There are no numeric
JSON fields in the immutable identity.

## Source and dependency evidence

Fixed paths relative to package root:

- `certification-source-manifest.json`
- `Backend/requirements.production.lock`
- `Backend/requirements.production.metadata.json`

The source manifest uses `flowsignal-certification-source/v1` with matching
`packaging_source_sha` and a non-empty `files` list. Each file has exactly
`path`, `size`, `sha256`, `mode`. Paths must be canonical relative POSIX paths;
duplicates, traversal, absolute paths, symlinks and incomplete entries fail.
No manifest/record/lock/metadata self-reference is permitted in `files`.
The manifest's other reviewed provenance/exclusion information is protected by
the independent hash. A pin is not inferred from that information.

Git modes `100644` and `100755` retain their content/executable semantics, but
installation must strip write permission: actual modes are `0444` and `0555`.
The adapter does not chmod anything. Evidence files and immediate directories
must not have write bits; source-directory ancestors within the package must
also have no write bits. Reads are bounded to 32 MiB per file.

Undeclared `.py`, `.pyc`, `.pyo`, `.so`, `.pyd`, `.pth` and `.zip` files beneath
the package root block verification. No package symlinks are allowed; root
`.git` bookkeeping is ignored. Disable bytecode generation in the future
release. Runtime state belongs outside the import/package tree. Non-executable
runtime text/data does not change the source hash, but this is not permission
to introduce writable import roots. Dependency lock bytes are verified; this
adapter is not an installed-wheel inventory or external-library attestation.

## Caller compatibility and rollout

`capture()` retains `backend_git_sha` (the packaging source SHA), `build_identity`
and `clean_source=True` only after evidence verification. `clean_source` means
verified packaged-source identity, not Git cleanliness. `identity_schema` and
`source_manifest_sha256` are explicit additions. `source_tree` is omitted:
a source-manifest SHA-256 is not a Git tree object. Existing worker and recovery
bootstrap consumers require no authorization or admission changes.

This checkout deliberately has no release record/expectations. It stays blocked.
The historical manifest `51e8540b1b2be9804ce2cbccbf753aa41c4f1eb041d7329312c29968a940af1b`
certifies the old source, not this adapter. Next packaging must derive the new
source SHA, generate and independently pin a NEW manifest containing this
adapter, and provision reviewed evidence separately. The certified dependency
lock and metadata bytes remain unchanged. No final OCI digest is invented here.
