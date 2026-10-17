import tempfile
import unittest
from pathlib import Path
from scripts.ci import redroid_app_smoke as app
from scripts.ci import redroid_prepare_apps as prepare


class ReDroidApplicationSmokeTest(unittest.TestCase):
    def test_memory_units_are_normalized(self):
        self.assertEqual(app.memory_mib('1.5GiB / 16GiB'), 1536)
        self.assertEqual(app.memory_mib('512MiB / 16GiB'), 512)
        with self.assertRaises(ValueError):
            app.memory_mib('')

    def test_local_assets_keep_both_qq_versions_without_source_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = prepare.write_profiles(Path('environment/redroid-smoke'), root)
            import json
            profiles = json.loads((root / 'assets/compatibility-local/qq/targets.json').read_text())
            self.assertEqual([p['identity']['versionName'] for p in profiles], ['9.3.1', '9.3.70'])
            self.assertTrue(all(p.get('sourceUrl') is None for p in profiles))
            self.assertTrue(all(p['verificationStatus'] == 'static-verified' for p in profiles))
            self.assertEqual(set(inputs), {'qq-old', 'qq', 'wechat', 'wecom'})
            self.assertEqual(set((root / 'resources/META-INF/xposed/scope.list').read_text().splitlines()),
                             {'com.tencent.mobileqq', 'com.tencent.mm', 'com.tencent.wework'})

    def test_only_the_official_single_signer_is_accepted(self):
        digest = 'a' * 64
        prepare.verify_signer('Signer #1 certificate SHA-256 digest: ' + digest, digest)
        prepare.verify_signer('V2 Signer: certificate SHA-256 digest: ' + digest, digest)
        prepare.verify_signer('V3.0 Signer: certificate SHA-256 digest: ' + digest, digest)
        with self.assertRaises(ValueError):
            prepare.verify_signer('Signer #1 certificate SHA-256 digest: ' + 'b' * 64, digest)
        with self.assertRaises(ValueError):
            prepare.verify_signer('Signer #1 certificate SHA-256 digest: ' + digest + '\nSigner #2 certificate SHA-256 digest: ' + digest, digest)
