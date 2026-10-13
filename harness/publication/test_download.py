import copy
import unittest
import download

class ArtifactPins(unittest.TestCase):
    def metadata(self):
        return dict(id=11776309299,name='tested-oci-1-38310701782',
            digest='sha256:f5286712b662010ce43ab7d5bcf2e48ca52ec111530b23f8402e9611f59d6a0a',
            expired=False,workflow_run=dict(id=38310701782,
                head_sha='ec7d81b1196bef1bdb2402fa8db2787e783f2b58'))
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
