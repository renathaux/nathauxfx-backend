"""Executable architecture rule for startup's deliberately narrow RO boundary."""
import ast


def check_startup(source, module):
    allowed = {
        ('legacy_adoption', '_transport'): {'read_original_transport', '_destination_preflight'},
        ('legacy_adoption', 'adopt_verified_cutover'): {'_VerificationTree'},
    }
    commands = {'inventory_state', 'copy_state', 'verify_state'}
    class Guard(ast.NodeVisitor):
        function = None

        def visit_FunctionDef(self, node):
            previous = self.function
            self.function = node.name
            self.generic_visit(node)
            self.function = previous

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Import(self, node):
            assert not any('cutover' in n.name for n in node.names), (module, node.lineno)

        def visit_ImportFrom(self, node):
            name = node.module or ''
            if name == 'startup_recovery.cutover_evidence':
                assert {n.name for n in node.names} <= allowed.get((module, self.function), set())
                assert all(n.asname is None for n in node.names)
            elif 'cutover' in name:
                # Data/error types and read-only PG snapshots are not commands.
                symbols = {
                    'startup_recovery.cutover_manifest': {'CutoverError'},
                    'startup_recovery.cutover_db': {'read_heads'},
                }
                assert module == 'legacy_adoption' and self.function == 'adopt_verified_cutover'
                assert {n.name for n in node.names} <= symbols.get(name, set())
                assert all(n.asname is None for n in node.names)
            else:
                assert not any(n.name.startswith('cutover') for n in node.names)

        def visit_Call(self, node):
            name = getattr(node.func, 'id', getattr(node.func, 'attr', ''))
            assert name not in commands
            if name in {'read_original_transport', '_destination_preflight', '_VerificationTree'}:
                assert name in allowed.get((module, self.function), set())
            # Generic CLI / dynamic command-module access is forbidden as well.
            assert not any(isinstance(x, ast.Constant) and isinstance(x.value, str)
                           and 'startup_recovery.cutover' in x.value for x in ast.walk(node))
            self.generic_visit(node)
    Guard().visit(ast.parse(source))


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
