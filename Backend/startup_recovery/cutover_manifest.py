"""Pinned transport evidence, never recovery authority. No filesystem/DB IO."""
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType

from startup_recovery.checkpoints import _digest, _encoded, _parse, _valid_hash


class CutoverError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ManifestDocument:
    raw: bytes
    digest: str


@dataclass(frozen=True)
class HeadSnapshot:
    raw: bytes
    digest: str


@dataclass(frozen=True)
class CutoverReport:
    operation: str
    manifest_digest: str | None
    copy_verified: bool
    ready_for_cutover: bool
    blockers: tuple[str, ...]
    operational_blockers: tuple[str, ...]
    remaining_unknowns: tuple[str, ...]
    copied_count: int
    reused_count: int


@dataclass(frozen=True)
class FileStat:
    device: int
    inode: int
    size: int
    mode: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int
    atime_ns: int


@dataclass(frozen=True)
class FileImage:
    raw: bytes
    stat: FileStat


@dataclass(frozen=True)
class Family:
    kind: str
    name: str | None
    classification: str
    trust_status: str


_STATE = ('live_backup', 'paper_backup', 'final_signal_hold', 'fifteen_m_swing_watch',
          'app_settings', 'feature_flags', 'market_data_source', 'ctrader_accounts',
          'news_trading_state', 'visits')
POLICY = MappingProxyType({
    **{n: Family(kind, kind+'.json', 'COPY_CANDIDATE', 'LEGACY_UNTRUSTED')
       for n, kind in enumerate(_STATE, 1)},
    11: Family('live_monthly_history', 'live_monthly_history.json', 'RECONSTRUCTABLE', 'EXCLUDED'),
    12: Family('generation', None, 'COPY_CANDIDATE', 'DB_REFERENCED_NOT_ADMITTED'),
    13: Family('unreferenced_generation', None, 'EVIDENCE_ONLY', 'EVIDENCE_ONLY'),
    14: Family('news_audit', 'news_trading_audit.jsonl', 'COPY_CANDIDATE', 'AUDIT_ONLY'),
    15: Family('users', 'users.json', 'UNKNOWN', 'UNKNOWN'),
    16: Family('trade_history_store', 'trade_history_store.json', 'UNKNOWN', 'UNKNOWN'),
    17: Family('credentials', '.env', 'EXCLUDED', 'EXCLUDED'),
    18: Family('sqlite', None, 'UNKNOWN', 'UNKNOWN'),
    **{n: Family(kind, None, 'CACHE', 'EXCLUDED') for n, kind in enumerate(
        ('candle_cache', 'history_cache', 'jobs', 'results', 'heavy_lock',
         'sqlite_lock', 'facts_pickle', 'bytecode'), 19)},
    27: Family('operator_outputs', None, 'UNKNOWN', 'UNKNOWN'),
})
BINDINGS = MappingProxyType({p.kind: p.name for p in POLICY.values() if p.name and p.classification == 'COPY_CANDIDATE'})
KINDS = frozenset((*_STATE, 'live_monthly_history'))
TOP_KEYS = frozenset(('schema', 'policy_version', 'source_root', 'state_root', 'scope',
                     'db_references', 'db_reference_sha256', 'records', 'blockers'))
RECORD_KEYS = frozenset(('family', 'kind', 'source_path', 'presence', 'size', 'sha256',
    'source_mode', 'destination_mode', 'classification', 'target_path', 'trust_status',
    'db_reference_keys', 'reason'))
REF_KEYS = frozenset(('scope_key', 'kind', 'manifest_hash', 'file_hash', 'generation',
                     'identity', 'admission_hash', 'broker', 'environment', 'account_id'))


def require(condition, code='MANIFEST_INVALID'):
    if not condition:
        raise CutoverError(code)


def path(value, *, absolute):
    require(isinstance(value, str) and value and '\x00' not in value)
    p = PurePosixPath(value)
    require(p.is_absolute() == absolute and '..' not in p.parts and str(p) == value)
    require(value not in ('/', '.', ''))
    return p


def separate(a, b):
    require(not a.is_relative_to(b) and not b.is_relative_to(a), 'ROOT_OVERLAP')


def checked_scope(scope):
    require(type(scope) is dict and set(scope) == {'schema', 'binding_overrides', 'family_locations', 'operator_outputs'})
    require(type(scope['schema']) is int and scope['schema'] == 1)
    overrides, locations = scope['binding_overrides'], scope['family_locations']
    require(type(overrides) is dict and type(locations) is dict and type(scope['operator_outputs']) is list)
    require(set(overrides) <= set(BINDINGS))
    seen = set()
    for kind, value in overrides.items():
        # Source relocation cannot turn credential/cache files into canonical state.
        require(path(value, absolute=True).name == BINDINGS[kind], 'SOURCE_BINDING_INVALID')
        require(value not in seen)
        seen.add(value)
    for key, values in locations.items():
        require(key in {str(n) for n in range(1, 28)} and type(values) is list)
        require(len(values) == len(set(values)))
        for value in values:
            path(value, absolute=True)
    for value in scope['operator_outputs']:
        path(value, absolute=True)


def checked_references(refs):
    require(type(refs) is list)
    seen = set()
    for ref in refs:
        require(type(ref) is dict and set(ref) == REF_KEYS)
        require(ref['kind'] in KINDS and type(ref['identity']) is dict)
        require(type(ref['generation']) is int and ref['generation'] > 0)
        for key in ('scope_key', 'broker', 'environment', 'account_id'):
            require(isinstance(ref[key], str) and bool(ref[key]))
        for key in ('manifest_hash', 'file_hash', 'admission_hash'):
            require(_valid_hash(ref[key]))
        key = (ref['scope_key'], ref['kind'])
        require(key not in seen, 'REFERENCE_CONFLICT')
        seen.add(key)
    return sorted(refs, key=lambda r: (r['scope_key'], r['kind']))


def _checked(body):
    require(type(body) is dict and set(body) == TOP_KEYS)
    require(type(body['schema']) is int and body['schema'] == 1 and body['policy_version'] == 'native-cutover-v1')
    source, target = path(body['source_root'], absolute=True), path(body['state_root'], absolute=True)
    separate(source, target)
    checked_scope(body['scope'])
    refs = checked_references(body['db_references'])
    require(_digest(_encoded(refs)) == body['db_reference_sha256'], 'REFERENCE_HASH_MISMATCH')
    require(type(body['blockers']) is list and all(isinstance(x, str) and x for x in body['blockers']))
    require(type(body['records']) is list)
    seen, targets = set(), set()
    refkeys = {r['scope_key'] + ':' + r['kind'] for r in refs}
    for r in body['records']:
        require(type(r) is dict and set(r) == RECORD_KEYS)
        require(type(r['family']) is int and r['family'] in POLICY)
        p = POLICY[r['family']]
        require(r['classification'] == p.classification and r['trust_status'] == p.trust_status, 'POLICY_MISMATCH')
        require(r['kind'] == p.kind)
        src = path(r['source_path'], absolute=True)
        key = (r['family'], r['source_path'])
        require(key not in seen, 'DUPLICATE_RECORD')
        seen.add(key)
        require(r['presence'] in ('PRESENT', 'ABSENT'))
        require(r['reason'] is None or isinstance(r['reason'], str))
        require(type(r['db_reference_keys']) is list and set(r['db_reference_keys']) <= refkeys)
        if r['family'] in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 14):
            expected = body['scope']['binding_overrides'].get(p.kind, str(source / p.name))
            require(str(src) == expected and r['target_path'] == p.name, 'SOURCE_BINDING_INVALID')
            require(not r['db_reference_keys'])
        elif r['family'] == 12:
            rel = path(r['target_path'], absolute=False)
            require(len(rel.parts) == 3 and rel.parts[0] == '.recovery-generations' and _valid_hash(rel.parts[1]))
            require(rel.name == 'manifest.json' or rel.name in {k+'.json' for k in KINDS})
            require(rel.parts[1] in {ref['manifest_hash'] for ref in refs})
            require(src == source / rel and r['presence'] == 'PRESENT' and r['db_reference_keys'])
        else:
            require(r['target_path'] is None and not r['db_reference_keys'])
        if p.classification == 'COPY_CANDIDATE':
            require(r['target_path'] not in targets, 'DESTINATION_CONFLICT')
            targets.add(r['target_path'])
        if r['presence'] == 'ABSENT' or p.classification != 'COPY_CANDIDATE':
            require(all(r[k] is None for k in ('size', 'sha256', 'source_mode', 'destination_mode')))
            if p.classification != 'COPY_CANDIDATE':
                require(bool(r['reason']))
        else:
            require(type(r['size']) is int and r['size'] >= 0 and _valid_hash(r['sha256']))
            require(type(r['source_mode']) is int and 0 <= r['source_mode'] <= 0o7777)
            require(type(r['destination_mode']) is int and r['destination_mode'] == 0o600)
    result = dict(body, db_references=refs, records=sorted(body['records'],
        key=lambda r: (r['family'], r['kind'], r['source_path'], r['target_path'] or '')),
        blockers=sorted(set(body['blockers'])))
    return result


def canonical_manifest(body):
    try:
        raw = _encoded(_checked(body))
        return ManifestDocument(raw, _digest(raw))
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        raise CutoverError('MANIFEST_INVALID') from None


def decode_manifest(raw, expected_digest):
    require(type(raw) is bytes and _valid_hash(expected_digest), 'MANIFEST_PIN_REQUIRED')
    require(_digest(raw) == expected_digest, 'MANIFEST_HASH_MISMATCH')
    try:
        document = canonical_manifest(_parse(raw))
        require(document.raw == raw, 'MANIFEST_NONCANONICAL')
        return document
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise CutoverError('MANIFEST_INVALID') from None
