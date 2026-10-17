import hashlib
from pathlib import Path
import tempfile
import unittest
from scripts.ci.redroid_environment_probe import checked_digest


class ReDroidEnvironmentProbeTest(unittest.TestCase):
    def test_rejects_modified_framework_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / 'framework.zip'
            asset.write_bytes(b'pinned framework')
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            self.assertEqual(digest, checked_digest(asset, digest))
            asset.write_bytes(b'replaced framework')
            with self.assertRaisesRegex(ValueError, 'Asset hash mismatch'):
                checked_digest(asset, digest)


if __name__ == '__main__':
    unittest.main()
