import copy
import unittest
import download

class ArtifactPins(unittest.TestCase):
    def metadata(self):
        return dict(id=11697286441,name='tested-oci-1-38124754867',
            digest='sha256:cf28f8e3f6eb2fc86d0a3b96b926df541019d0197d5da1a72e60b3dfe48a1e51',
            expired=False,workflow_run=dict(id=38124754867,
                head_sha='0864b3b41e88e7c3ca03e8cf54ed265ff0edeab4'))
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
