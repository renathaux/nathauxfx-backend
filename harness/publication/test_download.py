import copy
import unittest
import download

class ArtifactPins(unittest.TestCase):
    def metadata(self):
        return dict(id=11717090533,name='tested-oci-1-38176383880',
            digest='sha256:12da2c5e31b18cacd2828c47c43eea8981919d07969aad70288063aeaaf90ea8',
            expired=False,workflow_run=dict(id=38176383880,
                head_sha='28ce458f71ce4a13a2e6038cd798872ef47e8e96'))
    def test_exact_artifact(self):
        download.validate_metadata(self.metadata())
    def test_wrong_identity_or_expired_blocks(self):
        for key,value in [('id',0),('name','other'),('digest','sha256:bad'),('expired',True)]:
            item=self.metadata();item[key]=value
            with self.subTest(key=key),self.assertRaises(AssertionError):download.validate_metadata(item)
        for key,value in [('id',0),('head_sha','bad')]:
            item=self.metadata();item['workflow_run'][key]=value
            with self.subTest(key=key),self.assertRaises(AssertionError):download.validate_metadata(item)
    def test_zip_digest_must_match(self):
        download.validate_digest(download.ZIP_SHA)
        with self.assertRaises(AssertionError):download.validate_digest('0'*64)

if __name__=='__main__':unittest.main()
