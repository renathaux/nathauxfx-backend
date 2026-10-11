"""Read-only transport evidence; no commands or recovery authority.

Original evidence and exhaustive namespace accounting are separate predicates.
The adoption caller must explain extra generations separately before publishing.
This module never reads native source paths, creates files, or grants readiness.
"""
from pathlib import Path
from startup_recovery.checkpoints import _digest, _encoded, _parse, _valid_hash
from startup_recovery.cutover_files import ReadTree
from startup_recovery.cutover_manifest import POLICY, KINDS, decode_manifest, require, path
from startup_recovery.cutover_validation import validate_generation


def _locations(root, scope):
    """Fixed bindings plus explicit declarations; no environment/path discovery."""
    locations = {n: set() for n in POLICY}
    for n, policy in POLICY.items():
        if policy.name:
            locations[n].add(Path(scope['binding_overrides'].get(policy.kind, str(root/policy.name))))
    for key, values in scope['family_locations'].items():
        n = int(key)
        if POLICY[n].classification == 'COPY_CANDIDATE':
            # Generation root is fixed by the existing manifest contract. Flat
            # candidate relocation must use binding_overrides, not extra copies.
            allowed = {root/'.recovery-generations'} if n == 12 else locations[n]
            require(set(map(Path, values)) <= allowed, 'SOURCE_BINDING_INVALID')
        elif n == 13:
            for value in values:
                p = Path(value)
                require(p.parent == root/'.recovery-generations' and
                    (_valid_hash(p.name) or p.name.startswith('.tmp-')), 'SOURCE_BINDING_INVALID')
            locations[n].update(map(Path,values))
        else:
            locations[n].update(map(Path,values))
    locations[27].update(map(Path,scope['operator_outputs']))
    declared = [(n,p) for n,items in locations.items() for p in items]
    for index,(n,p) in enumerate(declared):
        require(p != root and not root.is_relative_to(p), 'SOURCE_BINDING_INVALID')
        if n != 13:
            require(not p.is_relative_to(root/'.recovery-generations'), 'SOURCE_BINDING_INVALID')
        # Cache/exclusion declarations cannot hide a known state filename.
        known = {policy.name for policy in POLICY.values() if policy.name}
        if n in (11,17):
            require(p.name == POLICY[n].name, 'SOURCE_CLASSIFICATION_CONFLICT')
        if n in range(18,28):
            require(p.name not in known, 'SOURCE_CLASSIFICATION_CONFLICT')
        if n in range(19,27):
            sqlite_suffixes = tuple(ext+sidecar for ext in ('.db','.sqlite','.sqlite3')
                                    for sidecar in ('','-journal','-wal','-shm'))
            require(not p.name.endswith(sqlite_suffixes), 'SOURCE_CLASSIFICATION_CONFLICT')
        for other,q in declared[:index]:
            require(not (p == q or p.is_relative_to(q) or q.is_relative_to(p)),
                    'SOURCE_CLASSIFICATION_CONFLICT')
    return locations


def _destination_preflight(target, files, *, complete=False):
    """Inspect the entire bounded destination namespace without following links.

    No unknown file/directory is tolerated or read. Missing expected files are
    allowed only before publication. A previous partial copy is not a success
    marker: every existing byte/mode and all inputs must pass again.
    """
    expected = dict(files, **{'.cutover/lock':(b'',0o600)})
    directories = {'.'}
    for name in expected:
        directories.update(str(p) for p in Path(name).parents)
    found = set()

    def visit(directory):
        for name in target.entries(directory):
            relative = name if directory=='.' else directory+'/'+name
            if relative in directories:
                visit(relative)  # descriptor walk requires private, owned directories
            else:
                require(relative in expected, 'DESTINATION_UNKNOWN_STATE')
                image = target.read(relative)
                raw,mode = expected[relative]
                require(image.raw==raw and image.stat.mode==mode, 'DESTINATION_CONFLICT')
                found.add(relative)
    visit('.')
    if complete:
        require(found==set(expected), 'PACKAGE_INCOMPLETE')


class _VerificationTree(ReadTree):
    """Private destination directory checks, without any CopyTarget capability."""
    _private_directories = True


def _verify_coverage(body):
    """Check declared policy coverage as data, never inspect native source paths."""
    require(not body['blockers'], 'INVENTORY_BLOCKED')
    scope = body['scope']
    require(set(map(str,range(13,28))) <= set(scope['family_locations']), 'INCOMPLETE_COVERAGE')
    locations = _locations(Path(body['source_root']),scope)  # lexical only
    for n, paths in locations.items():
        actual = {Path(r['source_path']) for r in body['records'] if r['family']==n}
        require(paths <= actual, 'INCOMPLETE_COVERAGE')
        if n not in (12,13,27):
            require(actual==paths, 'SOURCE_BINDING_INVALID')
    for record in body['records']:
        if record['classification']=='UNKNOWN':
            require(record['presence']=='ABSENT', 'UNKNOWN_AUTHORITY_STATE')


def _verify_destination(target, body, document, heads):
    """Read/validate exact installed bytes. No source or writable loader input."""
    images = _original_images(target, body)
    files = {name: (image.raw, image.stat.mode) for name, image in images.items()}
    files['.cutover/'+document.digest+'.json'] = (document.raw,0o400)
    _destination_preflight(target,files,complete=True)
    refs = _parse(heads.raw)
    keys = set()
    for digest in sorted({r['manifest_hash'] for r in refs}):
        prefix = '.recovery-generations/'+digest+'/'
        records = [r for r in body['records'] if r['family']==12 and r['target_path'].startswith(prefix)]
        names = {r['target_path'] for r in records}
        require(prefix+'manifest.json' in names, 'GENERATION_MISSING')
        raw = images[prefix+'manifest.json'].raw
        manifest = _parse(raw)
        require(type(manifest) is dict and type(manifest.get('files')) is dict
            and set(manifest['files']) <= KINDS, 'GENERATION_INVALID')
        require(names=={prefix+'manifest.json',*(prefix+k+'.json' for k in manifest['files'])},
            'GENERATION_INCOMPLETE')
        validated = validate_generation(raw,{k:images[prefix+k+'.json'].raw for k in manifest['files']},digest,heads)
        for record in records:
            require(sorted(record['db_reference_keys'])==list(validated), 'REFERENCE_CONFLICT')
        keys.update(validated)
    require(keys=={r['scope_key']+':'+r['kind'] for r in refs}, 'GENERATION_INCOMPLETE')
    return images


def read_original_transport(tree, manifest_path, expected_digest):
    """Prove original installed transport only, under the caller's shared lock.

    Extra namespace is NOT accepted by this predicate: callers must account for
    it separately. No authority/readiness is granted by this observation.
    """
    require(_valid_hash(expected_digest), 'MANIFEST_PIN_REQUIRED')
    location = Path(path(str(manifest_path), absolute=True))
    name = '.cutover/' + expected_digest + '.json'
    require(location == tree.root/name, 'MANIFEST_PATH_INVALID')
    image = tree.read(name)
    require(image.stat.mode == 0o400, 'MANIFEST_MODE_INVALID')
    document = decode_manifest(image.raw, expected_digest)
    body = _parse(document.raw)
    _verify_coverage(body)
    require(body['state_root'] == str(tree.root), 'DESTINATION_MISMATCH')
    require(_digest(_encoded(body['db_references'])) == body['db_reference_sha256'], 'REFERENCE_CHANGED')
    require(set(tree.entries('.cutover')) == {'lock', expected_digest+'.json'}, 'DESTINATION_UNKNOWN_STATE')
    for record in body['records']:
        if record['classification'] == 'COPY_CANDIDATE' and record['presence'] == 'ABSENT':
            absent = POLICY[record['family']].name
            if absent:
                require(absent not in tree.entries('.'), 'DESTINATION_UNKNOWN_STATE')
    images = _original_images(tree, body)
    lock = tree.read('.cutover/lock')
    require(lock.raw == b'' and lock.stat.mode == 0o600, 'DESTINATION_CONFLICT')
    images.update({name: image, '.cutover/lock': lock})
    expected = {n: (v.raw, v.stat.mode) for n, v in images.items()}
    require(tree.read(name) == image, 'MANIFEST_CHANGED')
    return document, images, expected


def _original_images(target, body):
    images = {}
    for record in body['records']:
        if record['classification'] != 'COPY_CANDIDATE' or record['presence'] == 'ABSENT':
            continue
        name = record['target_path']
        image = target.read(name)
        require(image.stat.size == record['size'] and _digest(image.raw) == record['sha256']
                and image.stat.mode == record['destination_mode'], 'DESTINATION_MISMATCH')
        images[name] = image
    return images
