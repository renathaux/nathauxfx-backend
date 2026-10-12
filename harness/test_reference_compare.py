"""Exact-record comparator regressions. No product imports."""
import copy
import json
from pathlib import Path
import unittest
from reference_compare import compare_field, bindings, compare_environment, compare_records, PATH_NODES

def env(root='/tmp/fs-amd64-cert.ABC123', copyname='diagnostic-broad-abc123'):
    return dict(cwd=f'{root}/{copyname}/source/Backend', executable=root+'/venv/bin/python',
        environment=dict(DIAGNOSTIC_EVIDENCE=root+'/evidence',
        PYTHONPATH=root+'/test-tools:'+root+'/harness',
        VERIFIER_TEST_PG_DSN='postgresql+psycopg2://cert_user@/verifier_test?host=/tmp/fs-verifier-pg.cert.ABC123&port=55439'),
        packages=[['pytest','8.4.2']], python='3.14.3', uid=501, gid=1000)

class ExactComparison(unittest.TestCase):
    def test_bound_path_only_and_reconstruction(self):
        a,b=bindings(env()),bindings(env('/tmp/fs-amd64-cert.DEF456','diagnostic-broad-def456'))
        old=a['source']+'/Backend/Backend/x.py: missing'
        new=b['source']+'/Backend/Backend/x.py: missing'
        proof=compare_field(old,new,'paths',a,b)
        self.assertEqual(proof['reconstructed'],new)
        for bad in [new.replace('Backend/Backend','Backend'),new+' extra',new.replace('missing','wrong')]:
            with self.assertRaises(ValueError): compare_field(old,bad,'paths',a,b)
    def test_fixture_address_only(self):
        a=b=bindings(env())
        old="expected 'TOKEN' in '' = <_pytest.logging.LogCaptureFixture object at 0xabc>.text"
        new=old.replace('0xabc','0xdef')
        self.assertEqual(compare_field(old,new,'fixture',a,b)['reconstructed'],new)
        for bad in [new.replace('TOKEN','OTHER'),new.replace("in ''","in 'payload'"),new+' 0x123']:
            with self.assertRaises(ValueError): compare_field(old,bad,'fixture',a,b)
    def test_semantics_always_exact(self):
        a=b=bindings(env())
        for old,new in [('price=1','price=2'),('uuid=abc','uuid=def'),('0xabc','0xdef'),("{'x':1}","{'x':2}")]:
            with self.assertRaises(ValueError): compare_field(old,new,'fixture',a,b)
    def test_unknown_context_exact(self):
        a=b=bindings(env())
        with self.assertRaises(ValueError): compare_field('0xabc','0xdef',None,a,b)
    def test_environment_metadata_exact(self):
        a=env(); b=env('/tmp/fs-amd64-cert.DEF456','diagnostic-broad-def456')
        compare_environment(a,b)
        b['packages']=[['pytest','9']]
        with self.assertRaises(ValueError): compare_environment(a,b)
    def test_unbound_roots_rejected(self):
        a=env(); a['executable']='/usr/bin/python'
        with self.assertRaises(ValueError): bindings(a)
    def test_multiple_unequal_length_slots_reverse(self):
        a,b=bindings(env()),bindings(env('/tmp/fs-amd64-cert.LONGERTOKEN','diagnostic-broad-longertoken'))
        old=a['source']+'/x '+a['root']+'/venv/bin/python'
        new=b['source']+'/x '+b['root']+'/venv/bin/python'
        proof=compare_field(old,new,'paths',a,b)
        self.assertTrue(proof['reverse_verified'])
        self.assertEqual(len(proof['replacements']),2)
    def test_record_identity_and_unknown_nodes(self):
        a=env(); b=env('/tmp/fs-amd64-cert.DEF456','diagnostic-broad-def456')
        old=dict(node=sorted(PATH_NODES)[0],status='failure',boundary='supported',message='',body=a['cwd']+'/x')
        new=dict(old,body=b['cwd']+'/x')
        compare_records([old],[new],a,b)
        for field,value in [('node','unknown'),('status','passed'),('boundary','outside')]:
            with self.assertRaises(ValueError): compare_records([old],[dict(new,**{field:value})],a,b)
        with self.assertRaises(ValueError): compare_records([old],[],a,b)
        with self.assertRaises(ValueError): compare_records([dict(old,node='unknown')],[dict(new,node='unknown')],a,b)
        for change in [new['body']+'/extra',new['body'].replace('/x','/y')]:
            with self.assertRaises(ValueError): compare_records([old],[dict(new,body=change)],a,b)
    def test_pinned_reference_full_record_comparison(self):
        root=Path(__file__).parent/'native-reference'
        read=lambda n:json.loads((root/n).read_bytes())
        rows=read('diagnostic-results.json')['cases']; environment=read('diagnostic-environment.json')
        compare_records(rows,rows,environment,environment)
        wrong=copy.deepcopy(rows); wrong[0]['body']='unexpected'
        with self.assertRaises(ValueError): compare_records(rows,wrong,environment,environment)

if __name__=='__main__': unittest.main()
