import copy
import unittest
import download

class ArtifactPins(unittest.TestCase):
    def metadata(self):
        return dict(id=11987665377,name='tested-oci-1-38756405521',
            digest='sha256:8c7063de2ca4cc163be82eaf8cfadd91ec1734ce1724131aa3cea7b2c1fffd38',
            expired=False,workflow_run=dict(id=38756405521,
                head_sha='cd6cbf85f295ed6e4c7f37582cf737171b4ad6ba'))
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
