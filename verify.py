"""Verify already-certified OCI bytes; never builds or executes the image."""
import email.parser,gzip,hashlib,io,json,os,re,sys,tarfile,urllib.request,zipfile
from pathlib import Path

IMAGE='sha256:4f11f091d8ba77192a81569b3e85e101dcccc329c641eddeabd160b13be6fb49'
CONFIG='sha256:fb148eb27d5c97c373131778f2a0e8430f6bfcfbac4584995dc5d7e9ecaa30c9'
BUILD='be6aa50c8a32a71cf7cc4eeccb54c661e3baf5127c8aaada7ec075d6357aa48d'
SOURCE='0dbbef69bb293833c299c1fe3e728454b3fa1c45'
MANIFEST='e6aa098027a0246d53ca0f2c6fe599b23125b376d5e33c260896efc02ab73fcd'
LOCK='1d8e368b5645db69cab33479c56d7db0827155898368a419fc80ab9819ee5736'
METADATA='8e24bda82fa3cc5bdcf21f19c86eec85eae74e24e115a402066bfafe884e5e89'
COMMIT='ca1a28704a79595f01c49ae05d9645eac2adda92'
ZIP='19f6afdfb8437f928a21792b3e503039555aed27db4bf4dd8ef3ea19f3cc9b79'
REPOSITORY='ghcr.io/renathaux/nathauxfx-backend'
def sha(raw):return hashlib.sha256(raw).hexdigest()
def canonical(obj):return json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()

def retrieve():
    class ArtifactRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            redirected=super().redirect_request(req,fp,code,msg,headers,newurl)
            redirected.remove_header('Authorization')
            return redirected
    req=urllib.request.Request('https://api.github.com/repos/renathaux/nathauxfx-backend/actions/artifacts/11431930860/zip',
        headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json'})
    with urllib.request.build_opener(ArtifactRedirect()).open(req) as src,open('evidence.zip','xb') as dst:
        while chunk:=src.read(1024*1024):dst.write(chunk)

def extract():
    assert sha(Path('evidence.zip').read_bytes())==ZIP,'CERTIFICATION_ZIP_MISMATCH'
    with zipfile.ZipFile('evidence.zip') as archive:
        for name in ('image-1.oci.tar','summary.json','runtime-1.json'):
            with archive.open(name) as src,open(name,'xb') as dst:
                while chunk:=src.read(1024*1024):dst.write(chunk)
    assert json.loads(Path('summary.json').read_bytes())['packaging_certification']=='PASS'

def verify(path):
    files={}
    with tarfile.open(path) as archive:
        def blob(digest):
            raw=archive.extractfile('blobs/sha256/'+digest[7:]).read()
            assert 'sha256:'+sha(raw)==digest,'BLOB_MISMATCH'
            return raw
        index=json.load(archive.extractfile('index.json'))
        assert len(index['manifests'])==1 and index['manifests'][0]['digest']==IMAGE,'IMAGE_MISMATCH'
        manifest=json.loads(blob(IMAGE));assert manifest['config']['digest']==CONFIG
        config=json.loads(blob(CONFIG));assert (config['os'],config['architecture'])==('linux','amd64')
        for n,layer in enumerate(manifest['layers']):
            compressed=blob(layer['digest']);raw=gzip.decompress(compressed)
            assert 'sha256:'+sha(raw)==config['rootfs']['diff_ids'][n],'DIFF_ID_MISMATCH'
            with tarfile.open(fileobj=io.BytesIO(raw)) as members:
                for member in members:
                    name=member.name.removeprefix('./')
                    relevant=name in ('app/build-record.json','opt/flowsignal-release/expectations.json',
                        'app/certification-source-manifest.json','app/Backend/requirements.production.lock',
                        'app/Backend/requirements.production.metadata.json') or (
                        name.startswith('opt/venv/lib/python3.14/site-packages/') and name.endswith('.dist-info/METADATA'))
                    if relevant:
                        assert member.isfile(),'EVIDENCE_NOT_REGULAR'
                        files[name]=members.extractfile(member).read()
        record=files['app/build-record.json'];assert sha(record)==BUILD
        assert files['opt/flowsignal-release/expectations.json']==record
        identity=json.loads(record)
        assert identity['packaging_source_sha']==SOURCE
        assert identity['source_manifest_sha256']==MANIFEST
        for name,digest in [('app/certification-source-manifest.json',MANIFEST),
            ('app/Backend/requirements.production.lock',LOCK),('app/Backend/requirements.production.metadata.json',METADATA)]:
            assert sha(files[name])==digest
        norm=lambda n:re.sub('[-_.]+','-',n).lower()
        expected={norm(p['name']):p['version'] for p in json.loads(files['app/Backend/requirements.production.metadata.json'])['packages']}
        actual={}
        for name,raw in files.items():
            if name.endswith('.dist-info/METADATA'):
                parsed=email.parser.BytesParser().parsebytes(raw)
                key=norm(parsed['Name']);assert key not in actual;actual[key]=parsed['Version']
        installer=actual.pop('pip',None)
        assert actual==expected and len(actual)==39 and actual['greenlet']=='3.5.6','PACKAGE_PROFILE_MISMATCH'
    return dict(image_digest=IMAGE,config_digest=CONFIG,ordered_layers=[l['digest'] for l in manifest['layers']],
        diff_ids=config['rootfs']['diff_ids'],platform='linux/amd64',build_identity=BUILD,
        application_package_count=39,application_packages=actual,installer_pip=installer)

if __name__=='__main__':
    operation=sys.argv[1]
    if operation=='retrieve':retrieve();extract()
    elif operation=='extract':extract()
    elif operation=='verify':print(json.dumps(verify(sys.argv[2]),sort_keys=True,indent=2))
    elif operation=='release':
        before=json.loads(Path('pre-push.json').read_bytes());after=json.loads(Path('post-push.json').read_bytes())
        assert before==after,'REGISTRY_COPY_CHANGED'
        evidence=dict(schema='flowsignal-image-release-evidence/v1',packaging_source_sha=SOURCE,source_manifest_sha256=MANIFEST,
            dependency_lock_sha256=LOCK,dependency_metadata_sha256=METADATA,certification_run=37505453177,
            certification_commit=COMMIT,certified_image_digest=IMAGE,config_digest=CONFIG,
            registry_repository=REPOSITORY,immutable_registry_reference=REPOSITORY+'@'+IMAGE,
            verification=after,image_rebuilt=False,real_broker_orders_sent=0)
        print(json.dumps(evidence,sort_keys=True,indent=2))
