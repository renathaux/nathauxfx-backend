"""Startup has no cutover command capability.

Legacy adoption retains its exact reviewed read-only call sites. Terminated
ready-owner handoff has a separate, explicit administrative read-only boundary;
it is not callable by ordinary startup. Neither exception grants cutover commands
or filesystem/checkpoint writers. The sole administrative DB mutation remains
the already-reviewed store.relinquish call, not acquisition or recovery startup.
Ordinary startup cannot import the administrative module at all: resolving a
local alias is not a substitute for excluding the capability at its import.
"""
import ast


def _static_string(node):
    """Fold only literal strings; never execute or resolve runtime values."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_string(node.left), _static_string(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        # No FormattedValue, even if its expression happens to look constant.
        if all(isinstance(part, ast.Constant) and isinstance(part.value, str)
               for part in node.values):
            return ''.join(part.value for part in node.values)
    return None


def check_startup(source, module):
    allowed = {
        ('legacy_adoption', '_transport'): {'read_original_transport', '_destination_preflight'},
        ('legacy_adoption', 'adopt_verified_cutover'): {'_VerificationTree'},
        ('owner_handoff', '_ready_authority'): {'_verify_coverage', 'decode_manifest'},
        ('owner_handoff', 'relinquish_terminated_ready_owner'): {'_VerificationTree'},
        ('owner_handoff', 'relinquish_terminated_runtime_owner'): {'_VerificationTree'},
        ('checkpoint_lineage', 'read_news_checkpoint'): {'_VerificationTree'},
        ('news_strategy_transition', 'apply_transition'): {'_VerificationTree'},
        ('news_strategy_transition', 'inspect_transition'): {'_VerificationTree'},
    }
    commands = {'inventory_state', 'copy_state', 'verify_state'}
    admin = {'_ready_authority', 'relinquish_terminated_ready_owner',
        '_runtime_safety', '_runtime_fresh', '_runtime_generation', '_runtime_provenance',
        '_runtime_lineage', '_runtime_authority', 'relinquish_terminated_runtime_owner'}
    # Closed import/call capabilities for these two functions only. In
    # particular, an unrelated symbol from checkpoints/store is NOT permission
    # to import a writer or ownership-acquisition function from that module.
    admin_imports = {
        '_ready_authority': {
            'sqlalchemy': {'select', 'or_'},
            'models': {'RecoveryLegacyAdoption'},
            'startup_recovery.adoption_evidence': {'KINDS', 'AdoptedEnvelope',
                'validate_adoption_receipt', 'checked_adopted_envelope'},
            'startup_recovery.legacy_adoption': {'_receipt', '_heads', 'classify_retry'},
            'startup_recovery.cutover_manifest': {'decode_manifest'},
            'startup_recovery.cutover_evidence': {'_verify_coverage'},
        },
        'relinquish_terminated_ready_owner': {
            'pathlib': {'Path'},
            'startup_recovery.cutover_evidence': {'_VerificationTree'},
        },
    }
    common_calls = {'require', '_digest', '_encoded', '_parse', '_valid_hash', 'asdict', 'dict'}
    admin_calls = {
        '_ready_authority': common_calls | {'select', 'or_', 'len', 'set', 'sorted', 'str',
            '_receipt', '_heads', 'AdoptedEnvelope', 'validate_adoption_receipt',
            'checked_adopted_envelope', 'classify_retry', 'decode_manifest', '_verify_coverage'},
        'relinquish_terminated_ready_owner': common_calls | {'isinstance', 'type', 'bool',
            'callable', 'getattr', 'safety_reader', 'safety', 'ManagerToken', 'Path',
            '_VerificationTree', 'reconciliation_uow', '_ready_authority',
            'HandoffEvidence', 'RelinquishmentResult', 'RecoveryError'},
    }
    admin_methods = {
        '_ready_authority': {'tree': {'entries', 'read'}, 'store': {'scope_key'},
            'session': {'scalars', 'all'}, 'select': {'where', 'execution_options'},
            'envelopes': {'items'}, 'projections': {'append'}, 'token': {'upper'}},
        'relinquish_terminated_ready_owner': {'tree': {'shared'}, 'doc': {'get'},
            'root': {'is_absolute'}, 'safety_reader': {'database'},
            'store': {'require_owner', '_unsettled', 'relinquish'}},
    }
    # Separate runtime-owner contract; no additional capability for the old path.
    admin_imports.update({
        '_runtime_safety': {},
        '_runtime_fresh': {'time': {'time', 'monotonic'}, 'math': {'isfinite'}},
        '_runtime_generation': {'sqlalchemy': {'select'}, 'models': {'RecoveryLegacyAdoption'},
            'startup_recovery.checkpoints': {'_KINDS', 'ProducedEnvelope', 'checked_envelope', 'CheckpointCandidate'},
            'startup_recovery.adoption_evidence': {'AdoptedEnvelope', 'checked_adopted_envelope',
                'validate_adoption_receipt', 'receipt_validated_candidates'},
            'startup_recovery.legacy_adoption': {'_receipt'}},
        '_runtime_provenance': {'sqlalchemy': {'select'}, 'models': {'RecoveryAttempt'}},
        '_runtime_lineage': {},
        '_runtime_authority': {'sqlalchemy': {'select'}, 'models': {'RecoveryCheckpointHead'},
            'startup_recovery.checkpoints': {'_KINDS'},
            'startup_recovery.news_strategy_compatibility': {'validate_lineage'},
            'startup_recovery.reconcile': {'DiscoverySnapshot', 'reconcile'}},
        'relinquish_terminated_runtime_owner': {'pathlib': {'Path'}, 'time': {'monotonic'},
            'startup_recovery.cutover_evidence': {'_VerificationTree'},
            'startup_recovery.reconcile': {'freeze', 'thaw'}},
    })
    admin_calls.update({
        '_runtime_safety': common_calls | {'sorted', 'safety'},
        '_runtime_fresh': common_calls | {'type', 'isfinite', 'time', 'monotonic'},
        '_runtime_generation': common_calls | {'type', 'set', 'bool', 'sorted', 'len',
            'ProducedEnvelope', 'checked_envelope', 'CheckpointCandidate', 'AdoptedEnvelope',
            'checked_adopted_envelope', 'select', '_receipt', 'validate_adoption_receipt', 'receipt_validated_candidates'},
        '_runtime_provenance': common_calls | {'type', 'select', 'len'},
        '_runtime_lineage': common_calls | {'set', 'type', '_runtime_generation', '_runtime_provenance'},
        '_runtime_authority': common_calls | {'select', 'bool', 'len', 'set', 'sorted',
            '_runtime_safety', '_runtime_lineage', 'reconcile', 'list', 'validate_lineage'},
        'relinquish_terminated_runtime_owner': common_calls | {'isinstance', 'type', 'bool',
            'callable', 'getattr', 'monotonic', 'thaw', 'freeze', 'safety_reader', '_runtime_safety',
            '_runtime_fresh', 'ManagerToken', 'Path', '_VerificationTree', 'reconciliation_uow',
            '_runtime_authority', 'HandoffEvidence', 'RelinquishmentResult', 'RecoveryError'},
    })
    admin_methods.update({
        '_runtime_safety': {},
        '_runtime_fresh': {'broker': {'get'}},
        '_runtime_generation': {'tree': {'read', 'entries'}, 'adopted': {'add'},
            'session': {'scalars', 'all'}, 'select': {'where', 'execution_options'}},
        '_runtime_provenance': {'token': {'upper'}, 'store': {'scope_key'},
            'session': {'scalars', 'all'}, 'select': {'where', 'execution_options'}},
        '_runtime_lineage': {'seen': {'add'}, 'store': {'scope_key'}, 'lineage': {'append'}},
        '_runtime_authority': {'store': {'require_owner', 'scope_key'},
            'session': {'scalars', 'all'}, 'select': {'where', 'execution_options'},
            'projections': {'append'}, 'DiscoverySnapshot': {'create'}, 'set': {'intersection'}},
        'relinquish_terminated_runtime_owner': {'doc': {'get'}, 'root': {'is_absolute'},
            'tree': {'shared'}, 'safety_reader': {'database'},
            'store': {'require_owner', '_unsettled', 'relinquish'}},
    })
    admin_edges = {
        'relinquish_terminated_ready_owner': {'_ready_authority'},
        'relinquish_terminated_runtime_owner': {'_runtime_safety', '_runtime_fresh', '_runtime_authority'},
        '_runtime_authority': {'_runtime_safety', '_runtime_lineage'},
        '_runtime_lineage': {'_runtime_generation', '_runtime_provenance'},
    }
    # Extraction preserves the closed read capability set. Administrative
    # wrappers may only delegate to the corresponding reader, not retain their
    # former broad implementation permissions or import arbitrary capabilities.
    reader_names = {'_runtime_generation': 'read_generation',
                    '_runtime_provenance': 'read_provenance',
                    '_runtime_lineage': 'read_lineage'}
    for old, new in reader_names.items():
        admin_imports[new] = admin_imports[old]
        admin_calls[new] = {reader_names.get(n, n) for n in admin_calls[old]} | {'scope_key'}
        admin_methods[new] = {k:v for k,v in admin_methods[old].items() if k != 'store'}
        admin_imports[old] = {'startup_recovery.checkpoint_lineage': {new}}
        admin_calls[old] = {new}
        admin_methods[old] = {}
    admin_imports['read_news_checkpoint'] = {
        'pathlib': {'Path'}, 'models': {'RecoveryAccount'},
        'startup_recovery.cutover_evidence': {'_VerificationTree'},
        'startup_recovery.news_strategy_compatibility': {'validate_lineage'}}
    admin_calls['read_news_checkpoint'] = {'require','len','scope_key','_VerificationTree',
        'Path','read_lineage','validate_lineage','_parse'}
    admin_methods['read_news_checkpoint'] = {'session': {'get'},'tree': {'shared'}}
    protected_names = set().union(*admin_calls.values())
    parsed = ast.parse(source)
    parents = {child: parent for parent in ast.walk(parsed) for child in ast.iter_child_nodes(parent)}

    def receiver_root(node):
        while isinstance(node, (ast.Call, ast.Attribute)):
            node = node.func if isinstance(node, ast.Call) else node.value
        return node.id if isinstance(node, ast.Name) else None

    class Guard(ast.NodeVisitor):
        function = None

        def visit_FunctionDef(self, node):
            if module == 'news_strategy_transition':
                assert node.name not in {'_runtime_safety', '_runtime_fresh'}
            previous = self.function
            assert not self.administrative()  # no hidden nested capability scope
            if module == 'owner_handoff' and node.name in admin:
                assert type(node) is ast.FunctionDef
            self.function = previous+'.'+node.name if previous else node.name
            self.generic_visit(node)
            self.function = previous

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

        def visit_arg(self, node):
            if module == 'news_strategy_transition':
                assert node.arg not in {'_runtime_safety', '_runtime_fresh'}
            self.generic_visit(node)

        def administrative(self):
            return (module == 'owner_handoff' and self.function in admin
                or module == 'checkpoint_lineage' and self.function in
                    {*reader_names.values(), 'read_news_checkpoint'})

        def transition_safety(self, name):
            return (module == 'news_strategy_transition' and name in {
                'validate_current_authority': {'_runtime_safety'},
                'apply_transition': {'_runtime_safety', '_runtime_fresh'},
                'inspect_transition': {'_runtime_fresh'},
            }.get(self.function,set()))

        def visit_Name(self, node):
            if module == 'news_strategy_transition' and isinstance(node.ctx, ast.Store):
                assert node.id not in {'_runtime_safety','_runtime_fresh'}
            if node.id in admin and isinstance(node.ctx, ast.Load):
                parent = parents.get(node)
                assert (isinstance(parent, ast.Call) and parent.func is node
                    and (module == 'owner_handoff' and node.id in admin_edges.get(self.function, set())
                         or self.transition_safety(node.id)))
            if module in {'owner_handoff', 'checkpoint_lineage'} and (self.function is None or self.administrative()) and isinstance(node.ctx, ast.Store):
                # Do not alias a forbidden capability under an approved name.
                assert node.id not in protected_names

        def visit_Constant(self, node):
            # Also inspect folded literal targets stored before a dynamic import.
            value = _static_string(node)
            if value is not None:
                assert 'news_strategy_transition' not in value
            if module != 'owner_handoff' and value is not None:
                assert 'startup_recovery.owner_handoff' not in value
                assert value not in {'owner_handoff', '.owner_handoff',
                    'relinquish_terminated_owner', *admin}
            self.generic_visit(node)

        visit_BinOp = visit_Constant
        visit_JoinedStr = visit_Constant

        def visit_Attribute(self, node):
            assert node.attr != 'news_strategy_transition'
            if self.administrative() and isinstance(node.value, ast.Name) and node.value.id == 'store':
                assert node.attr in admin_methods[self.function].get('store', set())
                assert isinstance(node.ctx, ast.Load)
            self.generic_visit(node)

        def visit_Import(self, node):
            if module == 'news_strategy_transition':
                assert not any((n.asname or n.name.split('.')[0]) in
                    {'_runtime_safety', '_runtime_fresh'} for n in node.names)
            assert not any('news_strategy_transition' in n.name.split('.') for n in node.names)
            assert not self.administrative()  # no module-wide capabilities
            assert not any('cutover' in n.name for n in node.names), (module, node.lineno)
            assert not any(n.name == 'startup_recovery.owner_handoff'
                           or n.name.startswith('startup_recovery.owner_handoff.')
                           for n in node.names), (module, node.lineno)

        def visit_ImportFrom(self, node):
            name = node.module or ''
            if module == 'news_strategy_transition':
                assert not any(n.asname in {'_runtime_safety','_runtime_fresh'} for n in node.names)
            assert 'news_strategy_transition' not in name.split('.')
            assert not any(n.name == 'news_strategy_transition' for n in node.names)
            # Resolve the imported capability, never its local alias. Relative
            # imports in the scanned startup_recovery package count identically.
            safety_import = (name == 'startup_recovery.owner_handoff' and node.level == 0
                and bool(node.names) and all(self.transition_safety(n.name) and n.asname is None for n in node.names))
            assert safety_import or not (name == 'startup_recovery.owner_handoff'
                        or name.startswith('startup_recovery.owner_handoff.')
                        or (node.level and name.split('.')[0] == 'owner_handoff'))
            if name == 'startup_recovery' or (node.level and not name):
                assert not any(n.name in {'owner_handoff', '*'} for n in node.names)
            if module in {'owner_handoff', 'checkpoint_lineage'}:
                assert not any(n.asname in protected_names for n in node.names)
            if self.administrative():
                assert node.level == 0
                assert {n.name for n in node.names} <= admin_imports[self.function].get(name, set())
                assert all(n.asname is None for n in node.names)
            # Administrative callables may not leak into startup through aliases.
            assert safety_import or not any(n.name in admin for n in node.names)
            if name == 'startup_recovery.cutover_evidence':
                assert {n.name for n in node.names} <= allowed.get((module, self.function), set())
                assert all(n.asname is None for n in node.names)
            elif 'cutover' in name:
                # Data/error types and read-only PG snapshots are not commands.
                symbols = {
                    'startup_recovery.cutover_manifest': {'CutoverError'},
                    'startup_recovery.cutover_db': {'read_heads'},
                }
                if module == 'owner_handoff' and self.function == '_ready_authority':
                    symbols = {'startup_recovery.cutover_manifest': {'decode_manifest'}}
                else:
                    assert module == 'legacy_adoption' and self.function == 'adopt_verified_cutover'
                assert {n.name for n in node.names} <= symbols.get(name, set())
                assert all(n.asname is None for n in node.names)
            else:
                assert not any(n.name.startswith('cutover') for n in node.names)

        def visit_Call(self, node):
            name = getattr(node.func, 'id', getattr(node.func, 'attr', ''))
            assert name not in commands
            if name in admin:
                assert (module == 'owner_handoff' and name in admin_edges.get(self.function, set())
                        or self.transition_safety(name))
            if self.administrative():
                if isinstance(node.func, ast.Name):
                    assert name in admin_calls[self.function]
                    if name == 'getattr':
                        assert ast.unparse(node) == "getattr(safety_reader, 'database', None)"
                else:
                    assert isinstance(node.func, ast.Attribute)
                    root = receiver_root(node.func.value)
                    allowed_get = (self.function == 'relinquish_terminated_ready_owner'
                        and ast.unparse(node.func) == '(account.handoff_evidence or {}).get')
                    assert allowed_get or name in admin_methods[self.function].get(root, set())
            if name in {'read_original_transport', '_destination_preflight', '_VerificationTree',
                        '_verify_coverage', 'decode_manifest'}:
                assert name in allowed.get((module, self.function), set())
            # Generic CLI / dynamic command-module access is forbidden as well.
            assert not any(value is not None
                           and ('startup_recovery.cutover' in value
                                or 'startup_recovery.owner_handoff' in value
                                or value in {'owner_handoff', '.owner_handoff',
                                    'relinquish_terminated_owner', *admin})
                           for value in (_static_string(x) for x in ast.walk(node)))
            self.generic_visit(node)
    Guard().visit(parsed)


def check_evidence(source):
    """No command/writer capability may enter the shared read-only module."""
    imports = {
        'pathlib': {'Path'},
        'startup_recovery.checkpoints': {'_digest', '_encoded', '_parse', '_valid_hash'},
        'startup_recovery.cutover_files': {'ReadTree'},
        'startup_recovery.cutover_manifest': {'POLICY', 'KINDS', 'decode_manifest', 'require', 'path'},
        'startup_recovery.cutover_validation': {'validate_generation'},
    }
    calls = {'Path', '_digest', '_encoded', '_parse', '_valid_hash', 'decode_manifest',
             'require', 'path', 'validate_generation', '_locations', '_original_images',
             '_verify_coverage', '_destination_preflight', 'visit', 'dict', 'set', 'str',
             'int', 'tuple', 'range', 'sorted', 'list', 'len', 'map', 'enumerate', 'type'}
    methods = {'add', 'update', 'get', 'items', 'values', 'is_relative_to',
               'startswith', 'endswith', 'read', 'entries'}
    for node in ast.walk(ast.parse(source)):
        assert not isinstance(node, ast.Import)
        if isinstance(node, ast.ImportFrom):
            assert {n.name for n in node.names} <= imports.get(node.module, set())
            assert all(n.asname is None for n in node.names)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id in calls
            else:
                assert isinstance(node.func, ast.Attribute) and node.func.attr in methods
