"""Disposable Docker tests; no production connections or state."""
import hashlib,json,os,subprocess,tarfile,tempfile,time
from pathlib import Path
BASE='ghcr.io/renathaux/nathauxfx-backend@sha256:4f11f091d8ba77192a81569b3e85e101dcccc329c641eddeabd160b13be6fb49'
IMAGE='flowsignal-disk-maintenance:cert'
E=Path('evidence')
def run(*args,check=True):
    p=subprocess.run(args,capture_output=True,text=True)
    if check and p.returncode: raise RuntimeError(f'{args[0]} failed: {p.stderr[:500]}')
    return p
def sha(raw): return hashlib.sha256(raw).hexdigest()
def oci(path):
    with tarfile.open(path) as t:
        def blob(d):
            b=t.extractfile('blobs/sha256/'+d.split(':',1)[1]).read()
            assert 'sha256:'+sha(b)==d
            return b
        idx=json.load(t.extractfile('index.json'));assert len(idx['manifests'])==1
        d=idx['manifests'][0]['digest'];m=json.loads(blob(d));assert 'layers' in m
        c=json.loads(blob(m['config']['digest']))
        assert c['os']=='linux' and c['architecture']=='amd64'
        for l in m['layers']:blob(l['digest'])
        return dict(manifest_digest=d,config_digest=m['config']['digest'],layers=[l['digest'] for l in m['layers']],diff_ids=c['rootfs']['diff_ids'])
def filesystem(image,label):
    cid=run('docker','create','--network','none',image).stdout.strip()
    try:run('docker','export','-o',str(E/(label+'.fs.tar')),cid)
    finally:run('docker','rm',cid)
    out={}
    with tarfile.open(E/(label+'.fs.tar')) as t:
        for m in t:
            name=m.name.removeprefix('./').rstrip('/')
            if name in ('app','opt/venv','opt/flowsignal-release') or name.startswith(('app/','opt/venv/','opt/flowsignal-release/')):
                out[name]=dict(mode=m.mode,uid=m.uid,gid=m.gid,mtime=m.mtime,size=m.size,type=m.type.decode(),link=m.linkname,pax=m.pax_headers,sha256=sha(t.extractfile(m).read()) if m.isfile() else None)
    assert all(p in out for p in ('app','opt/venv','opt/flowsignal-release'))
    (E/(label+'.protected.json')).write_text(json.dumps(out,sort_keys=True))
    (E/(label+'.fs.tar')).unlink()
    return out
def py(cid,source,user='501:1000'):
    return run('docker','exec','--user',user,cid,'/opt/venv/bin/python','-I','-B','-c',source)
def start(root=None,readonly=False):
    args=['docker','run','-d','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges']
    for cap in ('CHOWN','FOWNER','DAC_OVERRIDE','SETUID','SETGID'):args+=['--cap-add',cap]
    if root is not None:args+=['-v',str(root)+':/state'+(':ro' if readonly else '')]
    return run(*args,IMAGE).stdout.strip()
def health(cid):
    for _ in range(40):
        p=run('docker','exec','--user','501:1000',cid,'/opt/venv/bin/python','-I','-B','-c',"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:10000/',timeout=1).read().decode())",check=False)
        if p.returncode==0:
            value=json.loads(p.stdout)
            assert value==dict(maintenance=True,runtime_uid=501,runtime_gid=1000,state_uid=501,state_gid=1000,state_mode='0700')
            return value
        if run('docker','inspect','--format','{{.State.Status}}',cid).stdout.strip()=='exited':raise AssertionError(run('docker','logs',cid).stderr)
        time.sleep(.25)
    raise AssertionError('HEALTH_TIMEOUT')
def fixture(root,code):
    run('docker','run','--rm','--network','none','--user','0:0','-v',str(root)+':/state','--entrypoint','/opt/venv/bin/python',BASE,'-I','-B','-c',code)
def setup(root):fixture(root,"import os;os.chown('/state',0,1000);os.chmod('/state',0o2775)")
def failure(name,root=None,readonly=False):
    cid=start(root,readonly)
    try:
        assert run('docker','wait',cid).stdout.strip()=='1',name
        p=run('docker','logs',cid)
        assert p.stdout=='' and p.stderr=='MAINTENANCE_START_REJECTED\n',name
    finally:run('docker','rm','-f',cid)
def tests():
    result={}
    with tempfile.TemporaryDirectory(prefix='flowsignal-maintenance-cert-') as td:
        root=Path(td)/'state';root.mkdir();setup(root);cid=start(root)
        try:
            result['health']=health(cid)
            status=json.loads(py(cid,"import json;print(json.dumps(dict(line.rstrip().split(':',1) for line in open('/proc/1/status') if line.startswith(('Uid:','Gid:','Groups:','CapEff:')))))").stdout)
            assert status['Uid'].split()==['501']*4 and status['Gid'].split()==['1000']*4
            assert status['Groups'].strip()=='' and int(status['CapEff'].strip(),16)==0
            py(cid,"import os\ntry: os.setuid(0)\nexcept PermissionError: pass\nelse: raise AssertionError('root regained')")
            probe="import os,json;s=os.stat('/state');print(json.dumps([s.st_uid,s.st_gid,s.st_mode,s.st_ctime_ns]))"
            before=py(cid,probe).stdout;run('docker','restart',cid);health(cid);assert before==py(cid,probe).stdout
            result.update(empty_disk_ownership='PASS',privilege_drop='PASS',idempotence='PASS')
            helper='/usr/local/lib/flowsignal-maintenance/cutover_helper.py'
            p=run('docker','exec',cid,'/opt/venv/bin/python','-I','-B',helper,'sh',check=False)
            assert p.returncode==1 and p.stderr=='MAINTENANCE_HELPER_REJECTED\n'
            for op in ('inventory','copy','verify'):
                # Parser help only, never an operation or DB connection.
                p=run('docker','exec',cid,'/opt/venv/bin/python','-I','-B',helper,op,'--help')
                assert 'usage:' in p.stdout.lower()
            probe="""import runpy,os,sys
d=runpy.run_path('/usr/local/lib/flowsignal-maintenance/cutover_helper.py')
sys.argv=['helper','verify','--help']
def observe(path,args,env):
 assert os.getresuid()==(501,501,501) and os.getresgid()==(1000,1000,1000) and not os.getgroups()
 assert path=='/opt/venv/bin/python' and args[1:4]==['-B','-m','startup_recovery.cutover']
 assert os.getcwd()=='/app/Backend'
 print('HELPER_UID501_BEFORE_EXEC')
os.execve=observe
d['main']()
"""
            assert py(cid,probe,user='0:0').stdout.strip()=='HELPER_UID501_BEFORE_EXEC'
            result['cutover_helper']='PASS'
            prof=json.loads(py(cid,"import importlib.metadata as m,json;print(json.dumps({d.metadata['Name'].lower():d.version for d in m.distributions()},sort_keys=True))").stdout)
            tooling={k:prof.pop(k) for k in ('pip','setuptools','wheel') if k in prof}
            assert len(prof)==39 and prof['greenlet']=='3.5.6'
            result.update(application_packages=prof,installer_tooling=tooling)
            py(cid,"import os,pwd;assert os.stat('/root/.ssh').st_mode & 0o777 == 0o700;assert not os.listdir('/root/.ssh');assert pwd.getpwnam('root').pw_shell not in ('/usr/sbin/nologin','/bin/false');assert next(x.split(':')[1] for x in open('/etc/shadow') if x.startswith('root:'))=='NP'",user='0:0')
            result['ssh_metadata']='PASS'
        finally:run('docker','rm','-f',cid)
        failure('not_mounted');result['not_mounted']='PASS'
        setup(root);fixture(root,"from pathlib import Path;Path('/state/unexpected').write_bytes(b'test-only')")
        before=root.stat();failure('unexpected_file',root);after=root.stat()
        assert (before.st_uid,before.st_gid,before.st_mode,before.st_ctime_ns)==(after.st_uid,after.st_gid,after.st_mode,after.st_ctime_ns)
        result['unexpected_file']='PASS'
        empty=Path(td)/'readonly';empty.mkdir();setup(empty)
        before=empty.stat();failure('ownership_failure',empty,True);after=empty.stat()
        assert (before.st_uid,before.st_gid,before.st_mode,before.st_ctime_ns)==(after.st_uid,after.st_gid,after.st_mode,after.st_ctime_ns)
        result['ownership_failure']='PASS'
        fixture(root,"import os;os.unlink('/state/unexpected');os.chown('/state',"+str(os.getuid())+','+str(os.getgid())+");os.chmod('/state',0o700)")
    return result
if __name__=='__main__':
    import sys
    if sys.argv[1]=='test':
        assert filesystem(BASE,'base')==filesystem(IMAGE,'derived'),'PROTECTED_DIRECTORY_MUTATION'
        result=tests();result['protected_paths']='PASS'
        (E/'tests.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
        print('ALL_MAINTENANCE_TESTS_PASS')
    elif sys.argv[1]=='oci':print(json.dumps(oci(sys.argv[2]),sort_keys=True))
