import copy
import unittest
import download

class ArtifactPins(unittest.TestCase):
    def metadata(self):
        return dict(id=11907941907,name='tested-oci-1-38584096907',
            digest='sha256:f6bb7557ff6c23782fcac3ac435827594b2954e1a5acc6c3bb121b20d4aa8a26',
            expired=False,workflow_run=dict(id=38584096907,
                head_sha='8328d8025da84a47b56a4d6548126a496b1aa19a'))
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
