"""Reviewed native diagnostic reference gate; never bless a new result."""
import hashlib
import json
from pathlib import Path
import sys
from diagnostic_gate import run
from diagnostic_contract import canonical, digest
from reference_compare import compare_records, require

PINS={
 'collection-order.json':'fef6aa7e9387aa60dedb40986711673a2340c28d8018b7b0ed20b092a18fce3c',
 'diagnostic-environment.json':'01a61a22a8bd589115cad6ab99545a1c79dfe2df4e0face835ad7dc5943c9835',
 'diagnostic-results.json':'4799e356a3f11ac5968669c7bafe543633503ad88213b69ce96cc190aa0f71e0',
 'sqlite-before.json':'a91d83dc3ce1455fda8647e78c4fd4fbaa981b7c964d4760cebb9477378a7f11',
 'sqlite-after.json':'a91d83dc3ce1455fda8647e78c4fd4fbaa981b7c964d4760cebb9477378a7f11',
 'semantic-601666bc295a2ed33abee4114e852c94f97791cb869d644b5c41ecc7c994eadc.json':'d74317cc9680f0d7174a85ba76ae62ef22f1339d48d84b7e6f1687e058238987',
 'semantic-88ad865a2a67fb9276329a1e510a2767f10ae790eb18eaceb4e23ecd172be4ac.json':'dbcee72a6b85ef79d122cb3ff951ac7eb2de488e7342c9481e08c1d9264f49f9',
 'semantic-942d3bac49d5639d0a245123b36f5a279b1955f9b482903d5825bf71cea24712.json':'facb57b249c8784f98f7914a45761a33175308148c873122a290a538ac4edd31'}

def check(evidence):
    directory=Path(__file__).parent/'native-reference'
    reference={}
    for name,pin in PINS.items():
        raw=(directory/name).read_bytes()
        require(hashlib.sha256(raw).hexdigest()==pin,'REFERENCE_PIN_MISMATCH')
        reference[name]=json.loads(raw)
    def read(name):return json.loads((evidence/name).read_bytes())
    actual=read('diagnostic-results.json'); previous=reference['diagnostic-results.json']
    require(digest(previous['cases'])==previous['fingerprint_sha256']=='3b597f3a32de8b5245a0bb174dafe15e6a967bb7be7c717f84c170816b1581d3','RAW_REFERENCE_FINGERPRINT')
    require(digest(actual['cases'])==actual['fingerprint_sha256'],'RAW_CURRENT_FINGERPRINT')
    require(actual['process_returncode']==previous['process_returncode']==1,'BROAD_EXIT_DRIFT')
    for name in ('collection-order.json','sqlite-before.json','sqlite-after.json'):
        require(read(name)==reference[name],'DIAGNOSTIC_SETUP_OR_ORDER_DRIFT:'+name)
    require(read('diagnostic-cleanup.json')=={'disposable_copy_removed':True},'DISPOSABLE_CLEANUP')
    semantic_names={n for n in PINS if n.startswith('semantic-')}
    require({p.name for p in evidence.glob('semantic-*.json')}==semantic_names,'SEMANTIC_OBSERVATION_SET')
    for name in semantic_names:
        require(canonical(read(name))==canonical(reference[name]),'FULL_SEMANTIC_OBSERVATION_DRIFT')
    require(read('source-after.json')==dict(source_files=376,manifest_sha256='dbfa367c95c106c787b8338b282c2e83f4985cecb845ac3cb5bd8e233c32a0cc',source_sha='4c1c483086f08a8c2596cb400725071d03a1249e'),'SOURCE_DRIFT')
    proofs=compare_records(previous['cases'],actual['cases'],reference['diagnostic-environment.json'],read('diagnostic-environment.json'))
    result=dict(pass_comparability=True,pass_whole_repository=False,pass_release=False,
        historical_equivalence_claimed=False,reference_run=38270359938,
        current_raw_fingerprint=actual['fingerprint_sha256'],reference_raw_fingerprint=previous['fingerprint_sha256'],
        lossless_field_proofs=proofs,source_after=read('source-after.json'))
    # Existing downstream consumer expects 'pass': only the comparison gate.
    result['pass']=True
    result.update(new=[],missing=[],changed=[])
    (evidence/'broad-comparison.json').write_bytes(canonical(result))

if __name__=='__main__':
    root,evidence=map(lambda p:Path(p).resolve(),sys.argv[1:])
    run(root,evidence)
    check(evidence)
