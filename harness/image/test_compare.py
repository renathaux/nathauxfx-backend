import copy
import unittest
import compare as report
from compare import compare

class ReproducibilityTests(unittest.TestCase):
    def inputs(self):
        image=dict(image_digest='manifest',manifest_digest='manifest',config_digest='config',
            layers=['one','two'],diff_ids=['raw1','raw2'],architecture='linux/amd64',artifact_sha256='archive')
        return dict(source_sha='source',manifest_sha256='pin',source_files=369,dependency_lock='lock',
            build_identity='identity',production=image,maintenance=copy.deepcopy(image))

    def test_identical_full_identity_passes(self):
        a=self.inputs();b=copy.deepcopy(a)
        b['production']['artifact_sha256']='different archive wrapper'
        self.assertTrue(compare(a,b)['production_reproducible'])

    def test_every_required_divergence_blocks(self):
        for kind in ('production','maintenance'):
            for key in ('image_digest','manifest_digest','config_digest','layers','diff_ids','architecture'):
                with self.subTest(kind=kind,key=key):
                    a=self.inputs();b=copy.deepcopy(a);b[kind][key]='different'
                    with self.assertRaises(AssertionError):compare(a,b)
        a=self.inputs();b=copy.deepcopy(a);b['build_identity']='different'
        with self.assertRaises(AssertionError):compare(a,b)

    def test_backend_gate_required_in_final_report(self):
        backend={name:dict(tests=n,failures=0,errors=0,skipped=0) for name,n in
            [('database-config',7),('owner',71),('focused',281),('safe-recovery',439),('cutover',321),('bounded',787)]}
        self.assertTrue(hasattr(report,'validate_backend'),'backend evidence validation missing')
        self.assertEqual(report.validate_backend(backend),backend)
        backend['bounded']['errors']=1
        with self.assertRaises(AssertionError): report.validate_backend(backend)

if __name__=='__main__':unittest.main()
