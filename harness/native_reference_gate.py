"""Reviewed native diagnostic reference gate; never bless a new result."""
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from diagnostic_gate import run
from diagnostic_contract import canonical, digest, read_cases, classify
from reference_compare import compare_records, require
from verify_source import SOURCE, MANIFEST, COUNT
from observation_cases import NODES

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
    require(read_cases(evidence/'broad.xml')==actual['cases'],'XML_RAW_RECORD_MISMATCH')
    require(actual['process_returncode']==previous['process_returncode']==1,'BROAD_EXIT_DRIFT')
    old_order=reference['collection-order.json']
    freshness_raw=(Path(__file__).parent/'freshness-cases.json').read_bytes()
    require(hashlib.sha256(freshness_raw).hexdigest()=='30cc9fdcaa27b3e7685e1d43db1a3dbc374d1774b6c7cc07e8793d7206c93a50','FRESHNESS_MATRIX_PIN')
    freshness_order=json.loads(freshness_raw)
    require(len(freshness_order)==len(set(freshness_order))==123,'FRESHNESS_MATRIX_COUNT')
    freshness_added=[n for n in freshness_order if n not in old_order]
    require(len(freshness_added)==41,'FRESHNESS_ADDITIVE_COUNT')
    require([n for n in freshness_order if n in old_order]==[n for n in old_order if n in freshness_order], 'FRESHNESS_OLD_ORDER_DRIFT')
    additive=NODES+freshness_added
    require(not set(old_order).intersection(additive),'NEW_NODE_ALREADY_HISTORICAL')
    # Preserve every old relative ordering and exact parameter order. Insert only
    # the reviewed new modules at pytest's ordinary alphabetical module positions.
    expected_order=sorted([n for n in old_order if n not in freshness_order]+NODES+freshness_order,key=lambda node:node.split('::')[0])
    matrix_raw=(Path(__file__).parent/'news-compatibility-order.json').read_bytes()
    added_raw=(Path(__file__).parent/'news-compatibility-cases.json').read_bytes()
    require(hashlib.sha256(matrix_raw).hexdigest()=='c8d17a6fe7ffc6d06e8709e85e7f618cdd7d4cd1e799a8a89bc78828b1dd8430','NEWS_MATRIX_PIN')
    require(hashlib.sha256(added_raw).hexdigest()=='85b2c77397d53b5b43d123f85b0b9c4615e72d0c5fba22f7770d06ce6b4e3ed0','NEWS_ADDITIVE_PIN')
    matrix=json.loads(matrix_raw); news_added=json.loads(added_raw)
    require(len(matrix)==len(set(matrix))==329,'NEWS_MATRIX_COUNT')
    require(len(news_added)==len(set(news_added))==213,'NEWS_ADDITIVE_COUNT')
    require(not set(expected_order).intersection(news_added),'NEWS_ALREADY_EXISTING')
    require([n for n in matrix if n not in expected_order]==news_added,'NEWS_ADDITIVE_DRIFT')
    modules={n.split('::')[0] for n in matrix}
    require([n for n in matrix if n in expected_order]==[n for n in expected_order if n.split('::')[0] in modules],'NEWS_EXISTING_ORDER_DRIFT')
    expected_order=sorted([n for n in expected_order if n.split('::')[0] not in modules]+matrix,key=lambda node:node.split('::')[0])
    additive+=news_added
    require(read('collection-order.json')==expected_order,'DIAGNOSTIC_COLLECTION_DRIFT')
    new_cases=[]
    for case in ET.parse(evidence/'broad.xml').iter('testcase'):
        classname=case.get('classname','')
        node=classname.replace('.', '/')+'.py::'+case.get('name','')
        if node in additive:
            new_cases.append(node)
            require(not any(case.find(tag) is not None for tag in ('failure','error','skipped')),
                    'NEW_DIAGNOSTIC_TEST_NOT_PASS')
    require(new_cases==[n for n in expected_order if n in additive],'NEW_DIAGNOSTIC_TEST_EXECUTION_DRIFT')
    for name in ('sqlite-before.json','sqlite-after.json'):
        require(read(name)==reference[name],'DIAGNOSTIC_SETUP_OR_ORDER_DRIFT:'+name)
    require(read('diagnostic-cleanup.json')=={'disposable_copy_removed':True},'DISPOSABLE_CLEANUP')
    semantic_names={n for n in PINS if n.startswith('semantic-')}
    require({p.name for p in evidence.glob('semantic-*.json')}==semantic_names,'SEMANTIC_OBSERVATION_SET')
    for name in semantic_names:
        require(canonical(read(name))==canonical(reference[name]),'FULL_SEMANTIC_OBSERVATION_DRIFT')
    require(read('source-after.json')==dict(source_files=COUNT,manifest_sha256=MANIFEST,source_sha=SOURCE),'SOURCE_DRIFT')
    added_nodes={node.split('::')[0][:-3].replace('/','.')+'::'+node.split('::',1)[1] for node in additive}
    added=[row for row in actual['cases'] if row['node'] in added_nodes]
    expected_added=sorted([dict(node=node,status='passed',boundary=classify(node),message='',body='')
                           for node in added_nodes],key=lambda row:row['node'])
    require(added==expected_added,'ADDITIVE_RAW_RESULT_DRIFT')
    prior_cases=[row for row in actual['cases'] if row['node'] not in added_nodes]
    proofs=compare_records(previous['cases'],prior_cases,reference['diagnostic-environment.json'],read('diagnostic-environment.json'))
    result=dict(pass_comparability=True,pass_whole_repository=False,pass_release=False,
        historical_equivalence_claimed=False,reference_run=38270359938,
        current_raw_fingerprint=actual['fingerprint_sha256'],reference_raw_fingerprint=previous['fingerprint_sha256'],
        lossless_field_proofs=proofs,source_after=read('source-after.json'))
    result['reviewed_additive_tests']=dict(tests=len(additive),failures=0,errors=0,skipped=0)
    # Existing downstream consumer expects 'pass': only the comparison gate.
    result['pass']=True
    result.update(new=[],missing=[],changed=[])
    (evidence/'broad-comparison.json').write_bytes(canonical(result))

if __name__=='__main__':
    root,evidence=map(lambda p:Path(p).resolve(),sys.argv[1:])
    run(root,evidence)
    check(evidence)
