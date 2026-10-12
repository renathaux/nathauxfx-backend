import hashlib
from pathlib import Path
import tempfile
import unittest
from auth_overlay import auth_overlay, required_expand, INPUTS

class AuthOverlayTests(unittest.TestCase):
    def test_missing_required_pattern_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError):required_expand(Path(t),'missing*.py')
    def test_overlay_exact_bytes_outside_source_and_cleanup_on_failure(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/'source'; root.mkdir()
            with self.assertRaisesRegex(RuntimeError,'test interruption'):
                with auth_overlay(root,Path(__file__).parent) as inputs:
                    directory=inputs['directory']
                    self.assertFalse(directory.is_relative_to(root))
                    self.assertEqual({p.name for p in directory.iterdir()},set(INPUTS))
                    self.assertEqual(inputs['test'].name,'test_strategy_studio_owner_boundary.py')
                    for name,pin in INPUTS.items():
                        self.assertEqual(hashlib.sha256((directory/name).read_bytes()).hexdigest(),pin['sha256'])
                    raise RuntimeError('test interruption')
            self.assertFalse(directory.exists())
            self.assertEqual(list(root.iterdir()),[])
    def test_tampered_overlay_input_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t); root=base/'source'; root.mkdir()
            inputs=base/'harness/broad-inputs/Backend/tests'; inputs.mkdir(parents=True)
            for name in INPUTS:(inputs/name).write_bytes(b'changed')
            with self.assertRaises(ValueError):
                with auth_overlay(root,base/'harness'):self.fail('bad input accepted')
            self.assertEqual(sorted(p.name for p in base.iterdir()),['harness','source'])

if __name__=='__main__':unittest.main()
