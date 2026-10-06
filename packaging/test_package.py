"""Packaging guard tests; never import the application or contact a broker."""
import importlib.util
import hashlib
import io
import json
from pathlib import Path
import tempfile
import tarfile
import unittest


class PackageTest(unittest.TestCase):
    def test_credential_scan_rejects_literals_without_exposing_values(self):
        import sys
        sys.path.insert(0,str(Path(__file__).parent))
        import credential_scan
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'module.py').write_text('SECRET_KEY = "synthetic-forbidden-literal"\n')
            result=credential_scan.scan(root)
            self.assertEqual(result['blocking_matches'],1)
            self.assertNotIn('synthetic-forbidden-literal',json.dumps(result))
            (root/'module.py').write_text('import os\nSECRET_KEY = os.environ.get("SECRET_KEY")\n')
            self.assertEqual(credential_scan.scan(root)['blocking_matches'],0)
            (root/'.env').write_text('synthetic')
            self.assertGreater(credential_scan.scan(root)['blocking_matches'],0)

    def api(self):
        path = Path(__file__).with_name('package.py')
        self.assertTrue(path.exists(), 'Packaging validation implementation missing')
        spec = importlib.util.spec_from_file_location('package_under_test',path)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        return mod

    def test_record_exact_identity(self):
        mod=self.api()
        record=mod.build_record()
        self.assertEqual(record['packaging_source_sha'],'0dbbef69bb293833c299c1fe3e728454b3fa1c45')
        self.assertEqual(record['source_manifest_sha256'],'e6aa098027a0246d53ca0f2c6fe599b23125b376d5e33c260896efc02ab73fcd')
        self.assertEqual(record['python_version'],'3.14.3')
        self.assertEqual(record['certified_dependency_run_id'],'37414992580')
        self.assertEqual(len(record),9)
        self.assertNotIn('image_digest',record)
        self.assertEqual(mod.canonical(record),mod.canonical(dict(reversed(list(record.items())))))

    def test_unpinned_manifest_rejected_before_copy(self):
        mod=self.api()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'certification-source-manifest.json').write_text('{}')
            with self.assertRaises(ValueError):mod.validate_source(root)

    def test_path_escape_rejected(self):
        mod=self.api()
        for name in ('../escape','/absolute','a/../b','a//b','.git/config','.env','Backend/data/live_backup.json'):
            with self.subTest(name=name), self.assertRaises(ValueError):mod.safe_path(name)

    def test_strict_profile(self):
        mod=self.api()
        expected={'greenlet':'3.5.6','example':'1'}
        self.assertTrue(mod.compare_profile(expected,expected))
        for actual in ({'greenlet':'3.5.5','example':'1'},dict(expected,extra='1'),{'greenlet':'3.5.6'}):
            with self.assertRaises(ValueError):mod.compare_profile(expected,actual)

    def test_oci_digest_distinct_from_config(self):
        mod=self.api()
        config=b'{"architecture":"amd64","os":"linux"}'
        config_digest='sha256:'+hashlib.sha256(config).hexdigest()
        manifest=json.dumps({'config':{'digest':config_digest},'layers':[]}).encode()
        manifest_digest='sha256:'+hashlib.sha256(manifest).hexdigest()
        index=json.dumps({'manifests':[{'digest':manifest_digest}]}).encode()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'image.tar'
            for corrupt in (False,True):
                with tarfile.open(path,'w') as archive:
                    for name,data in [('index.json',index),('blobs/sha256/'+manifest_digest[7:],manifest),
                        ('blobs/sha256/'+config_digest[7:],b'corrupt' if corrupt else config)]:
                        info=tarfile.TarInfo(name);info.size=len(data);archive.addfile(info,io.BytesIO(data))
                if corrupt:
                    with self.assertRaises(ValueError):mod.oci_identity(path)
                else:
                    result=mod.oci_identity(path)
                    self.assertEqual(result['image_digest'],manifest_digest)
                    self.assertEqual(result['manifest_digest'],manifest_digest)
                    self.assertEqual(result['config_digest'],config_digest)
                    self.assertEqual(result['oci_index_sha256'],hashlib.sha256(index).hexdigest())


if __name__=='__main__': unittest.main()
