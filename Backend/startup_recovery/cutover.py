"""Explicit operator CLI only. Import/help acquire no application capabilities."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import re
import sys


# Do not reflect arbitrary exception/manifest text, paths, or payloads as reasons.
_CODES = frozenset('''BUSY CUTOVER_FAILED CUTOVER_BLOCKED CLI_ARGUMENTS_INVALID
CUTOVER_DATABASE_CONFIGURATION_FAILED CUTOVER_DATABASE_IDENTITY_MISMATCH
CUTOVER_DATABASE_READ_FAILED CUTOVER_DATABASE_URL_INVALID CUTOVER_DATABASE_URL_REQUIRED
CUTOVER_POSTGRESQL_REQUIRED CUTOVER_REFERENCE_INVALID CONTENT_INVALID COPY_FAILED
DESTINATION_CONFLICT DESTINATION_MODE_INVALID DESTINATION_NOT_ALLOWED DIRECTORY_CHANGED
FILE_CHANGED NOATIME_OR_ACCESS_DENIED NOATIME_UNAVAILABLE PACKAGE_INCOMPLETE ROOT_CHANGED
TREE_CLOSED UNSAFE_ANCESTOR UNSAFE_DIRECTORY UNSAFE_FILE UNSAFE_MODE UNSAFE_OWNERSHIP
UNSAFE_PATH DUPLICATE_RECORD MANIFEST_HASH_MISMATCH MANIFEST_INVALID MANIFEST_NONCANONICAL
MANIFEST_PIN_REQUIRED POLICY_MISMATCH REFERENCE_CONFLICT REFERENCE_HASH_MISMATCH
ROOT_OVERLAP SOURCE_BINDING_INVALID COPY_NOT_VERIFIED CUTOVER_MANIFEST_HASH_MISMATCH
CUTOVER_MANIFEST_INVALID DESTINATION_CHANGED DESTINATION_MISMATCH DESTINATION_UNKNOWN_STATE
FIRST_START_VERIFICATION_REQUIRED GENERATION_INCOMPLETE GENERATION_INVALID GENERATION_MISSING
INCOMPLETE_COVERAGE INVENTORY_BLOCKED LEGACY_CUTOVER_REQUIRED LEGACY_RECONCILIATION_REQUIRED
MANIFEST_CHANGED MANIFEST_MODE_INVALID RECOVERY_RECONCILIATION_REQUIRED REFERENCE_CHANGED
SOURCE_CHANGED SOURCE_CLASSIFICATION_CONFLICT SOURCE_OR_REFERENCE_CHANGED
UNKNOWN_AUTHORITY_STATE UNMAPPED_GENERATION_BINDING CUTOVER_ADMISSION_MISMATCH
CUTOVER_CHECKPOINT_REJECTED CUTOVER_ENVELOPE_INVALID CUTOVER_FILE_HASH_MISMATCH
CUTOVER_GENERATION_CONFLICT CUTOVER_GENERATION_INVALID CUTOVER_GENERATION_UNREFERENCED
CUTOVER_IDENTITY_INVALID CUTOVER_IDENTITY_MISMATCH CUTOVER_PARENT_INVALID
CUTOVER_PAYLOADS_INCOMPLETE CUTOVER_REFERENCE_CONFLICT CUTOVER_REFERENCE_HASH_MISMATCH
CUTOVER_REFERENCE_NONCANONICAL CUTOVER_SCOPE_INVALID PHYSICAL_SEPARATION_UNAVAILABLE'''.split())


class _ArgumentsInvalid(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error echoes potentially sensitive untrusted argv.
        raise _ArgumentsInvalid()


def _parser():
    parser = _Parser(prog='python -B -m startup_recovery.cutover', allow_abbrev=False,
                     description='Explicit state transport; never recovery admission.')
    sub = parser.add_subparsers(dest='operation', required=True)
    inventory = sub.add_parser('inventory', allow_abbrev=False)
    for name in ('source-root', 'state-root', 'scope', 'evidence-root'):
        inventory.add_argument('--'+name, required=True)
    for name in ('copy', 'verify'):
        command = sub.add_parser(name, allow_abbrev=False)
        command.add_argument('--manifest', required=True)
        command.add_argument('--manifest-sha256', required=True)
    return parser


def _codes(values):
    return sorted({code if code in _CODES else 'CUTOVER_BLOCKED'
                   for value in values for code in [value.split(':', 1)[0]]})


def _execute(args):
    # Lazy, read-capability-only dependency graph. No ORM/application bootstrap.
    from startup_recovery.cutover_db import configured_engine
    from startup_recovery.cutover_files import CopyTarget, ReadTree
    from startup_recovery.cutover_manifest import checked_scope, decode_manifest, path
    from startup_recovery.checkpoints import _parse
    from startup_recovery.cutover_service import inventory_preflight, inventory_state, copy_state, verify_state

    engine = configured_engine()  # Explicit config required before any file access.
    try:
        if args.operation == 'inventory':
            scope_path = path(args.scope, absolute=True)
            with ReadTree(scope_path.parent) as tree:
                scope = _parse(tree.read(scope_path.name).raw)
            checked_scope(scope)
            inventory_preflight(args.source_root,args.state_root,scope,args.evidence_root)
            with ReadTree(args.source_root) as source, CopyTarget(args.evidence_root) as evidence:
                document, report = inventory_state(source, args.state_root, scope, engine, evidence)
            manifest_path = str(Path(args.evidence_root)/(document.digest+'.json'))
        else:
            manifest_path = str(path(args.manifest, absolute=True))
            if args.operation == 'copy':
                location = Path(manifest_path)
                with ReadTree(location.parent) as tree:
                    document = decode_manifest(tree.read(location.name).raw, args.manifest_sha256)
                report = copy_state(document, engine, expected_digest=args.manifest_sha256)
            else:
                report = verify_state(manifest_path, engine, expected_digest=args.manifest_sha256)
        return dict(operation=args.operation, manifest_sha256=report.manifest_digest,
                    manifest_path=manifest_path, copy_verified=report.copy_verified,
                    ready_for_cutover=report.ready_for_cutover, blockers=_codes(report.blockers),
                    operational_blockers=_codes(report.operational_blockers),
                    remaining_unknowns=['UNKNOWN_STATE'] if report.remaining_unknowns else [],
                    copied_count=report.copied_count, reused_count=report.reused_count)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    result = dict(operation=None, manifest_sha256=None, manifest_path=None,
                  copy_verified=False, ready_for_cutover=False, blockers=[],
                  operational_blockers=[], remaining_unknowns=[], copied_count=0, reused_count=0)
    try:
        args = _parser().parse_args(argv)
        result['operation'] = args.operation
        if args.operation != 'inventory' and not re.fullmatch('[0-9a-f]{64}', args.manifest_sha256):
            raise _ArgumentsInvalid()
        # Dependencies cannot leak their diagnostics to the operator response.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = _execute(args)
    except SystemExit as exc:
        return int(exc.code)  # argparse help only; no database/filesystem imports.
    except _ArgumentsInvalid:
        result['blockers'] = ['CLI_ARGUMENTS_INVALID']
    except Exception as exc:
        from startup_recovery.cutover_manifest import CutoverError
        result['blockers'] = _codes([exc.code]) if isinstance(exc, CutoverError) else ['CUTOVER_FAILED']
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    if 'BUSY' in result['blockers']:
        return 3
    return 2 if (result['blockers'] or result['operational_blockers'] or result['remaining_unknowns']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
