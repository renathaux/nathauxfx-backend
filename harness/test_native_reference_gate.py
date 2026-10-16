import json
from pathlib import Path
import shutil
import tempfile
import unittest
from native_reference_gate import check, PINS
from verify_source import SOURCE, MANIFEST, COUNT
from observation_cases import NODES
import xml.etree.ElementTree as ET
from diagnostic_contract import read_cases, digest

class ReferenceGateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in PINS:
            shutil.copyfile(Path(__file__).parent/'native-reference'/name,self.root/name)
        self.write('diagnostic-cleanup.json',{'disposable_copy_removed':True})
        self.write('source-after.json',{'source_files':COUNT,'manifest_sha256':MANIFEST,'source_sha':SOURCE})
        old=json.loads((self.root/'collection-order.json').read_bytes())
        freshness=json.loads((Path(__file__).parent/'freshness-cases.json').read_bytes())
        order=sorted([n for n in old if n not in freshness]+NODES+freshness,
                     key=lambda n:n.split('::')[0])
        news=json.loads((Path(__file__).parent/'news-compatibility-order.json').read_bytes())
        modules={n.split('::')[0] for n in news}
        order=sorted([n for n in order if n.split('::')[0] not in modules]+news,
                     key=lambda n:n.split('::')[0])
        self.write('collection-order.json',order)
        xml=ET.Element('testsuites'); suite=ET.SubElement(xml,'testsuite')
        for row in json.loads((self.root/'diagnostic-results.json').read_bytes())['cases']:
            classname,name=row['node'].split('::',1)
            case=ET.SubElement(suite,'testcase',classname=classname,name=name)
            if row['status']!='passed':
                child=ET.SubElement(case,row['status'],message=row['message']); child.text=row['body']
        for node in order:
            if node in old:
                continue
            path,name=node.split('::',1)
            ET.SubElement(suite,'testcase',classname=path[:-3].replace('/','.'),name=name)
        ET.ElementTree(xml).write(self.root/'broad.xml')
        self.sync_results()
    def sync_results(self):
        result=json.loads((self.root/'diagnostic-results.json').read_bytes())
        result['cases']=read_cases(self.root/'broad.xml')
        result['fingerprint_sha256']=digest(result['cases'])
        self.write('diagnostic-results.json',result)
    def write(self,name,value): (self.root/name).write_text(json.dumps(value))
    def test_reference_self_match(self): check(self.root)
    def test_order_drift_blocks(self):
        self.write('collection-order.json',[])
        with self.assertRaises(ValueError):check(self.root)
    def test_sqlite_drift_blocks(self):
        self.write('sqlite-before.json',{})
        with self.assertRaises(ValueError):check(self.root)
    def test_cleanup_failure_blocks(self):
        self.write('diagnostic-cleanup.json',{'disposable_copy_removed':False})
        with self.assertRaises(ValueError):check(self.root)
    def test_raw_fingerprint_drift_blocks(self):
        p=self.root/'diagnostic-results.json'; v=json.loads(p.read_bytes())
        v['cases'][0]['body']='different'; self.write(p.name,v)
        with self.assertRaises(ValueError):check(self.root)
    def test_undisplayed_dictionary_drift_blocks(self):
        for p in self.root.glob('semantic-*.json'):
            value=json.loads(p.read_bytes())
            if 'current' in value:
                value['current']['undisplayed_extra_field']='changed'
                self.write(p.name,value)
                break
        else:self.fail('No pinned full payload')
        with self.assertRaises(ValueError):check(self.root)
    def test_missing_semantic_observation_blocks(self):
        next(self.root.glob('semantic-*.json')).unlink()
        with self.assertRaises(ValueError):check(self.root)
    def test_new_test_skipped_blocks(self):
        xml=ET.parse(self.root/'broad.xml')
        ET.SubElement(list(xml.iter('testcase'))[-1],'skipped')
        xml.write(self.root/'broad.xml')
        self.sync_results()
        with self.assertRaises(ValueError):check(self.root)
    def test_new_test_missing_blocks(self):
        xml=ET.parse(self.root/'broad.xml')
        suite=next(xml.iter('testsuite')); suite.remove(list(suite.iter('testcase'))[-1])
        xml.write(self.root/'broad.xml')
        self.sync_results()
        with self.assertRaises(ValueError):check(self.root)
    def test_duplicate_new_test_blocks(self):
        import copy
        xml=ET.parse(self.root/'broad.xml'); suite=next(xml.iter('testsuite'))
        suite.append(copy.deepcopy(list(suite.iter('testcase'))[-1]))
        xml.write(self.root/'broad.xml'); self.sync_results()
        with self.assertRaises(ValueError):check(self.root)
    def test_new_failure_blocks(self):
        xml=ET.parse(self.root/'broad.xml')
        ET.SubElement(list(xml.iter('testcase'))[-1],'failure',message='regression')
        xml.write(self.root/'broad.xml'); self.sync_results()
        with self.assertRaises(ValueError):check(self.root)
    def test_historical_semantic_failure_change_blocks(self):
        xml=ET.parse(self.root/'broad.xml')
        child=next(xml.iter('failure')); child.text='different semantic failure'
        xml.write(self.root/'broad.xml'); self.sync_results()
        with self.assertRaises(ValueError):check(self.root)
    def test_old_relative_order_change_blocks(self):
        p=self.root/'collection-order.json'; nodes=json.loads(p.read_bytes())
        nodes[0],nodes[1]=nodes[1],nodes[0]; self.write(p.name,nodes)
        with self.assertRaises(ValueError):check(self.root)

if __name__=='__main__':unittest.main()
