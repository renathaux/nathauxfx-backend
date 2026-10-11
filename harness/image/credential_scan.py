"""Bounded static packaging scan. Findings never contain matched values."""
import ast
import json
from pathlib import Path
import re
import sys

SECRET = re.compile(r'(^|_)(api_?key|admin_?token|access_?token|refresh_?token|bearer_?token|password|passwd|secret|secret_?key|client_?secret|private_?key|database_?url|dsn|connection_?string)$',re.I)
SIGNATURE = re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}|-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----')
URL = re.compile(r'[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@',re.I)

def scan(root):
    root=Path(root);findings=[];checked=0;synthetic=0
    def add(path,line,kind):findings.append(dict(path=path,line=line,kind=kind))
    for path in sorted(root.rglob('*')):
        name=path.relative_to(root).as_posix()
        if path.is_symlink():add(name,0,'SYMLINK');continue
        if any(p in ('.git','__pycache__') or p.startswith('.env') for p in path.relative_to(root).parts):
            add(name,0,'FORBIDDEN_STATE');continue
        if not path.is_file():continue
        if path.suffix in ('.db','.sqlite','.sqlite3','.pyc','.pickle'):
            add(name,0,'RUNTIME_ARTIFACT');continue
        try:text=path.read_text()
        except UnicodeDecodeError:add(name,0,'UNREVIEWED_BINARY');continue
        checked+=1
        # Synthetic credential fixtures are already byte-pinned by validate_source.
        fixture=name.startswith('Backend/tests/')
        for number,line in enumerate(text.splitlines(),1):
            if SIGNATURE.search(line) or URL.search(line):
                if fixture:synthetic+=1
                else:add(name,number,'CREDENTIAL_SIGNATURE')
        if path.suffix!='.py':continue
        tree=ast.parse(text)
        def literal(key,value,line):
            nonlocal synthetic
            if not isinstance(value,ast.Constant) or not isinstance(value.value,str) or not value.value:return
            if value.value==key and re.fullmatch('[A-Z][A-Z0-9_]+',key):return
            if value.value.startswith(('http://','https://')) and not URL.search(value.value):return
            if fixture:synthetic+=1
            else:add(name,line,'CREDENTIAL_LITERAL')
        for node in ast.walk(tree):
            if isinstance(node,(ast.Assign,ast.AnnAssign)):
                for target in node.targets if isinstance(node,ast.Assign) else [node.target]:
                    if isinstance(target,ast.Name) and SECRET.search(target.id):literal(target.id,node.value,node.lineno)
            if isinstance(node,ast.Dict):
                for key,value in zip(node.keys,node.values):
                    if isinstance(key,ast.Constant) and isinstance(key.value,str) and SECRET.search(key.value):literal(key.value,value,node.lineno)
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr in ('get','getenv') and len(node.args)>1:
                key=node.args[0]
                if isinstance(key,ast.Constant) and isinstance(key.value,str) and SECRET.search(key.value):literal(key.value,node.args[1],node.lineno)
    return dict(schema='packaging-static-credential-scan/v1',checked_files=checked,blocking_matches=len(findings),findings=findings,
        pinned_synthetic_fixture_matches=synthetic,limits='Static signatures and credential-position literals; not proof against arbitrary encoded secrets. No live credential checks.')

if __name__=='__main__':
    result=scan(sys.argv[1]);print(json.dumps(result,sort_keys=True));sys.exit(bool(result['blocking_matches']))
