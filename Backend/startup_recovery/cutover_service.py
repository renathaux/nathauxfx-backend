"""Explicit cutover inventory. File transport evidence never grants authority."""
from pathlib import Path

from startup_recovery.checkpoints import _digest, _encoded, _parse, _valid_hash
from startup_recovery.cutover_db import read_heads
from startup_recovery.cutover_files import ReadTree, CopyTarget, separate_storage
from startup_recovery.cutover_manifest import (
    BINDINGS, KINDS, POLICY, CutoverError, CutoverReport, canonical_manifest,
    checked_scope, decode_manifest, path, require, separate,
)
from startup_recovery.cutover_validation import validate_generation


def _present(location):
    """Observe only the entry name, never open an excluded file/directory."""
    try:
        with ReadTree(location.parent) as parent:
            return location.name in parent.entries('.')
    except FileNotFoundError:
        return False


def _record(family, location, present, *, image=None, target=None, keys=(), reason=None):
    policy = POLICY[family]
    return dict(family=family, kind=policy.kind, source_path=str(location),
        presence='PRESENT' if present else 'ABSENT',
        size=image.stat.size if image else None, sha256=_digest(image.raw) if image else None,
        source_mode=image.stat.mode if image else None, destination_mode=0o600 if image else None,
        classification=policy.classification, target_path=target, trust_status=policy.trust_status,
        db_reference_keys=list(keys), reason=reason)


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


def inventory_preflight(source_root, destination_root, scope, evidence_root):
    """Validate all writer separation BEFORE constructing/provisioning CopyTarget."""
    root, destination, evidence_root = map(Path, (source_root,destination_root,evidence_root))
    checked_scope(scope)
    locations = [p for values in _locations(root,scope).values() for p in values]
    separate_storage(destination, [root, *locations])
    separate_storage(evidence_root, [root, destination, *locations])


def inventory_state(source, destination_root, scope, engine, evidence):
    """Only explicit inventory writes evidence; collecting observations is read-only.

    Call inventory_preflight before constructing a writer; repeat before install.
    """
    inventory_preflight(source.root,destination_root,scope,evidence.root)
    document, report = _inventory_document(source,destination_root,scope,engine)
    inventory_preflight(source.root,destination_root,scope,evidence.root)
    evidence.install(document.digest+'.json',document.raw,0o400)
    return document, report


def _inventory_document(source, destination_root, scope, engine):
    """Return immutable evidence + a non-authorizing report; never fill STATE_ROOT.

    Empty explicit family declarations attest coverage, not observed absence.
    Families with no concrete path/reference therefore have no invented file
    record. Their declaration (or incomplete-coverage blocker) remains in the
    manifest; the fixed policy version supplies their classification.
    """
    checked_scope(scope)
    scope = _parse(_encoded(scope))  # request-local copy, exact numeric codec
    scope['family_locations'] = {k:sorted(v) for k,v in scope['family_locations'].items()}
    scope['operator_outputs'] = sorted(scope['operator_outputs'])
    root = Path(path(str(source.root), absolute=True))
    destination = Path(path(str(destination_root), absolute=True))
    separate(root,destination)
    # Validate the destination path without enumerating, reading or provisioning it.
    try:
        with ReadTree(destination):
            pass
    except FileNotFoundError:
        pass
    locations = _locations(root,scope)
    for paths in locations.values():
        for location in paths:
            separate(location,destination)
    heads = read_heads(engine)  # one all-scope snapshot; failure cannot mean no heads
    refs = _parse(heads.raw)
    manifest_hashes = {r['manifest_hash'] for r in refs}
    require(not any(p.name in manifest_hashes for p in locations[13]), 'SOURCE_CLASSIFICATION_CONFLICT')
    records, blockers, unknowns, operational = [], set(), set(), {'COPY_NOT_VERIFIED'}
    for n in range(13,28):
        if str(n) not in scope['family_locations']:
            blockers.add(f'INCOMPLETE_COVERAGE:{n}')
            unknowns.add(f'INCOMPLETE_COVERAGE:{n}')

    def blocked(code, location):
        blockers.add(code+':'+str(location))

    def observe(n,location):
        policy = POLICY[n]
        try:
            present = _present(location)
            if policy.classification == 'COPY_CANDIDATE':
                image = None
                if present:
                    with ReadTree(location.parent) as parent:
                        image = parent.read(location.name)
                records.append(_record(n,location,present,image=image,target=policy.name))
                if present and policy.trust_status == 'LEGACY_UNTRUSTED':
                    operational.add('LEGACY_RECONCILIATION_REQUIRED')
            else:
                reason = 'UNKNOWN_AUTHORITY_STATE' if policy.classification == 'UNKNOWN' else 'POLICY_'+policy.classification
                records.append(_record(n,location,present,reason=reason))
                if present and policy.classification == 'UNKNOWN':
                    blocked(reason,location)
                    unknowns.add(str(location))
        except (CutoverError, FileNotFoundError) as exc:
            blocked(exc.code if isinstance(exc,CutoverError) else 'SOURCE_CHANGED',location)

    # Only root/binding-parent runtime directories are enumerated for surprises.
    # Explicit excluded paths are presence-only; cache trees are never traversed.
    review_directories = {root}
    review_directories.update(p.parent for n,paths in locations.items()
                              if POLICY[n].classification == 'COPY_CANDIDATE' for p in paths)
    recognized = {p for paths in locations.values() for p in paths}
    recognized.add(root/'.recovery-generations')
    for directory in sorted(review_directories):
        try:
            with ReadTree(directory) as tree:
                names = tree.entries('.')
            for name in names:
                entry = directory/name
                # A declared descendant does not certify its containing directory
                # or unknown siblings. Block rather than widening the scan.
                if entry not in recognized:
                    descendants = [p for p in recognized if p.is_relative_to(entry)]
                    if descendants:
                        blockers.add('INCOMPLETE_DIRECTORY_COVERAGE:'+str(entry))
                        unknowns.add(str(entry))
                    else:
                        locations[27].add(entry)
        except FileNotFoundError:
            pass
        except CutoverError as exc:
            blocked(exc.code,directory)

    generation_root = root/'.recovery-generations'
    try:
        for name in source.entries('.recovery-generations'):
            if name in manifest_hashes:
                continue
            if _valid_hash(name) or name.startswith('.tmp-'):
                locations[13].add(generation_root/name)
            else:
                locations[27].add(generation_root/name)
    except FileNotFoundError:
        pass
    except CutoverError as exc:
        blocked(exc.code,generation_root)

    for n,paths in locations.items():
        for location in sorted(paths):
            observe(n,location)

    for digest in sorted(manifest_hashes):
        prefix = '.recovery-generations/'+digest
        selected = [r for r in refs if r['manifest_hash'] == digest]
        try:
            # The schema pins generation source paths under source_root. Never
            # silently read a different binding parent's generation as this one.
            for ref in selected:
                binding = scope['binding_overrides'].get(ref['kind'])
                require(binding is None or Path(binding).parent == root, 'UNMAPPED_GENERATION_BINDING')
            manifest_image = source.read(prefix+'/manifest.json')
            require(_digest(manifest_image.raw) == digest, 'CUTOVER_MANIFEST_HASH_MISMATCH')
            manifest = _parse(manifest_image.raw)
            require(type(manifest) is dict and type(manifest.get('files')) is dict and
                    set(manifest['files']) <= KINDS, 'CUTOVER_MANIFEST_INVALID')
            images = {kind:source.read(prefix+'/'+kind+'.json') for kind in sorted(manifest['files'])}
            keys = validate_generation(manifest_image.raw,{k:v.raw for k,v in images.items()},digest,heads)
            expected_names = {'manifest.json',*(k+'.json' for k in images)}
            for name in source.entries(prefix):
                if name not in expected_names:
                    observe(27,root/prefix/name)
            records.append(_record(12,root/prefix/'manifest.json',True,image=manifest_image,
                target=prefix+'/manifest.json',keys=keys))
            for kind,image in images.items():
                records.append(_record(12,root/prefix/(kind+'.json'),True,image=image,
                    target=prefix+'/'+kind+'.json',keys=keys))
        except FileNotFoundError:
            blocked('GENERATION_MISSING',root/prefix)
        except CutoverError as exc:
            blocked(exc.code,root/prefix)
        except (ValueError,TypeError,KeyError,UnicodeError):
            blocked('GENERATION_INVALID',root/prefix)

    document = canonical_manifest(dict(schema=1,policy_version='native-cutover-v1',
        source_root=str(root),state_root=str(destination),scope=scope,
        db_references=refs,db_reference_sha256=heads.digest,records=records,blockers=sorted(blockers)))
    return document, CutoverReport('inventory',document.digest,False,False,
        tuple(sorted(blockers)),tuple(sorted(operational)),tuple(sorted(unknowns)),0,0)


def _source_images(body):
    """Pin exact candidate observations for this invocation, including stat identity.

    Excluded payloads are never consumed. Whole-inventory comparison separately
    checks their declared presence/absence and all unknown-state coverage.
    """
    images = {}
    for record in body['records']:
        if record['classification'] != 'COPY_CANDIDATE':
            continue
        location = Path(record['source_path'])
        present = _present(location)
        require(present == (record['presence']=='PRESENT'), 'SOURCE_CHANGED')
        if not present:
            continue
        with ReadTree(location.parent) as parent:
            image = parent.read(location.name)
        require(image.stat.size==record['size'] and image.stat.mode==record['source_mode']
                and _digest(image.raw)==record['sha256'], 'SOURCE_CHANGED')
        images[record['target_path']] = image
    return images


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


def copy_state(document, engine, *, expected_digest=None):
    """Explicit raw transport only. No startup, admission, writer, or broker path.

    expected_digest must be independently supplied by the operator/caller, never
    inferred from the filename or document.digest. This routine does not create
    a durable success marker. Its positive report describes this observation
    only; later first-start verification is still required.
    """
    decoded = decode_manifest(document.raw,expected_digest)
    require(document.digest==decoded.digest, 'MANIFEST_HASH_MISMATCH')
    body = _parse(decoded.raw)
    require(not body['blockers'], 'INVENTORY_BLOCKED')
    protected = [Path(body['source_root']),
        *(p for paths in _locations(Path(body['source_root']),body['scope']).values() for p in paths)]
    separate_storage(body['state_root'], protected)
    with ReadTree(body['source_root']) as source:
        current, _ = _inventory_document(source,body['state_root'],body['scope'],engine)
        require(current.raw==decoded.raw, 'SOURCE_OR_REFERENCE_CHANGED')
        images = _source_images(body)
        files = {r['target_path']:(images[r['target_path']].raw,r['destination_mode'])
                 for r in body['records'] if r['target_path'] in images}
        files['.cutover/'+decoded.digest+'.json'] = (decoded.raw,0o400)
        # Source/reference/schema/policy blockers were checked before provisioning
        # the private diagnostic directory. No canonical file is installed until
        # the full destination set has passed preflight under its diagnostic lock.
        separate_storage(body['state_root'], protected)
        with CopyTarget(body['state_root']) as target, target.exclusive():
            _destination_preflight(target,files)
            copied = reused = 0
            for name,(raw,mode) in sorted(files.items()):
                require(_source_images(body)==images, 'SOURCE_CHANGED')
                result = target.install(name,raw,mode)
                copied += result=='CREATED'
                reused += result=='IDENTICAL'
            require(_source_images(body)==images, 'SOURCE_CHANGED')
            _destination_preflight(target,files,complete=True)
            # Fresh all-scope PostgreSQL read after publication. Rebuild only an
            # in-memory observation for comparison; never replace pinned evidence.
            final, _ = _inventory_document(source,body['state_root'],body['scope'],engine)
            require(final.raw==decoded.raw, 'SOURCE_OR_REFERENCE_CHANGED')
            require(_source_images(body)==images, 'SOURCE_CHANGED')
    operational = {'LEGACY_CUTOVER_REQUIRED','FIRST_START_VERIFICATION_REQUIRED'}
    if any(r['presence']=='PRESENT' and r['trust_status']=='LEGACY_UNTRUSTED' for r in body['records']):
        operational.add('LEGACY_RECONCILIATION_REQUIRED')
    return CutoverReport('copy',decoded.digest,True,False,(),tuple(sorted(operational)),(),copied,reused)


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
    images, files = {}, {}
    for record in body['records']:
        if record['classification']!='COPY_CANDIDATE' or record['presence']=='ABSENT':
            continue
        name = record['target_path']
        image = target.read(name)
        require(image.stat.size==record['size'] and _digest(image.raw)==record['sha256']
            and image.stat.mode==record['destination_mode'], 'DESTINATION_MISMATCH')
        images[name] = image
        files[name] = (image.raw,record['destination_mode'])
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


def verify_state(manifest_path, engine, *, expected_digest=None):
    """Verify pinned transport evidence, not readiness. All filesystem access is RO.

    The exact manifest path and independent pin are explicit inputs. Native paths
    in the manifest are lexical policy evidence only; native source may be offline.
    An existing diagnostic shared lock is read, never provisioned. No recovery
    loader, authority-granting lock, writer, broker, or source fallback is used.
    """
    require(_valid_hash(expected_digest), 'MANIFEST_PIN_REQUIRED')
    location = Path(path(str(manifest_path),absolute=True))
    try:
        with ReadTree(location.parent) as evidence:
            original = evidence.read(location.name)
            require(original.stat.mode==0o400, 'MANIFEST_MODE_INVALID')
            document = decode_manifest(original.raw,expected_digest)
            body = _parse(document.raw)
            _verify_coverage(body)
            with _VerificationTree(body['state_root']) as target, target.shared():
                heads = read_heads(engine)
                require(heads.raw==_encoded(body['db_references']) and heads.digest==body['db_reference_sha256'],
                    'REFERENCE_CHANGED')
                before = _verify_destination(target,body,document,heads)
                require(read_heads(engine)==heads, 'REFERENCE_CHANGED')
                require(_verify_destination(target,body,document,heads)==before, 'DESTINATION_CHANGED')
                require(evidence.read(location.name)==original, 'MANIFEST_CHANGED')
    except FileNotFoundError:
        raise CutoverError('PACKAGE_INCOMPLETE') from None
    except (ValueError,TypeError,KeyError,UnicodeError):
        raise CutoverError('GENERATION_INVALID') from None
    operational = {'LEGACY_CUTOVER_REQUIRED','RECOVERY_RECONCILIATION_REQUIRED'}
    if any(r['presence']=='PRESENT' and r['trust_status']=='LEGACY_UNTRUSTED' for r in body['records']):
        operational.add('LEGACY_RECONCILIATION_REQUIRED')
    return CutoverReport('verify',document.digest,True,False,(),tuple(sorted(operational)),(),0,len(before)+1)
