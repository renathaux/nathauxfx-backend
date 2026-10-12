import json
from pathlib import Path
import shutil
import tempfile
import unittest
from native_reference_gate import check, PINS

class ReferenceGateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in PINS:
            shutil.copyfile(Path(__file__).parent/'native-reference'/name,self.root/name)
        self.write('diagnostic-cleanup.json',{'disposable_copy_removed':True})
        self.write('source-after.json',{'source_files':376,'manifest_sha256':'dbfa367c95c106c787b8338b282c2e83f4985cecb845ac3cb5bd8e233c32a0cc','source_sha':'4c1c483086f08a8c2596cb400725071d03a1249e'})
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

if __name__=='__main__':unittest.main()
