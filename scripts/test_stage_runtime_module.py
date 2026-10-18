import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.ci.stage_runtime_module import stage_module


class StageRuntimeModuleTests(unittest.TestCase):
    def test_release_bytes_and_recorded_hash_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = 'impad-0.1.0-release.apk'
            data = b'exact release APK'
            digest = hashlib.sha256(data).hexdigest()
            (root / name).write_bytes(data)
            (root / 'build.json').write_text(json.dumps({'sha256': {name: digest}}))
            self.assertEqual(stage_module(root), digest)
            self.assertEqual((root / 'app-debug.apk').read_bytes(), data)
            self.assertEqual(list(root.glob('*.apk')), [root / 'app-debug.apk'])

    def test_existing_adaptation_module_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'app-debug.apk').write_bytes(b'adapted module')
            stage_module(root)
            self.assertEqual((root / 'app-debug.apk').read_bytes(), b'adapted module')

    def test_wrong_recorded_hash_is_rejected_before_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'release.apk').write_bytes(b'changed APK')
            (root / 'build.json').write_text(json.dumps({'sha256': {'release.apk': '0' * 64}}))
            with self.assertRaises(ValueError):
                stage_module(root)
            self.assertFalse((root / 'app-debug.apk').exists())
            self.assertTrue((root / 'release.apk').is_file())

    def test_missing_or_ambiguous_apk_is_rejected(self):
        for count in (0, 2):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for index in range(count):
                    (root / f'{index}.apk').write_bytes(b'APK')
                with self.assertRaises(ValueError):
                    stage_module(root)


if __name__ == '__main__':
    unittest.main()
