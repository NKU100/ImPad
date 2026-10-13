import unittest
from scripts.ci.discovery_gate import discovery_decision


class DiscoveryGateTest(unittest.TestCase):
    def setUp(self):
        self.url = 'https://dldir1v6.qq.com/weixin/android/weixin8079android3200_arm64.apk'
        self.digest = 'a' * 64
        self.profile = {'sourceUrl': self.url, 'verificationStatus': 'runtime-verified-local',
                        'identity': {'apkSha256': self.digest, 'versionCode': 3200}}

    def test_failed_candidate_without_new_input_waits(self):
        self.assertEqual('WAITING_FOR_INPUT', discovery_decision([], self.url, self.digest, False, 'wecom-package-arm64-83835-' + self.digest))
        self.assertEqual('ANALYZE_CANDIDATE', discovery_decision([], self.url, 'b'*64, False, 'wecom-package-arm64-83835-' + self.digest))

    def test_exact_supported_apk_skips_heavy_jobs(self):
        for status in ('runtime-verified-local', 'runtime-verified-hosted'):
            self.profile['verificationStatus'] = status
            self.assertEqual('SKIP_SUPPORTED_BUILD', discovery_decision([self.profile], self.url, self.digest, False))

    def test_unknown_or_static_only_build_requires_analysis(self):
        self.assertEqual('ANALYZE_CANDIDATE', discovery_decision([], self.url, self.digest, False))
        self.profile['verificationStatus'] = 'static-verified'
        self.assertEqual('ANALYZE_CANDIDATE', discovery_decision([self.profile], self.url, self.digest, False))

    def test_same_source_repack_with_new_digest_requires_analysis(self):
        self.assertEqual('ANALYZE_CANDIDATE', discovery_decision([self.profile], self.url, 'b' * 64, False))

    def test_manual_force_can_recheck_known_build(self):
        self.assertEqual('FORCE_RECHECK', discovery_decision([self.profile], self.url, self.digest, True))

    def test_duplicate_source_cannot_skip(self):
        with self.assertRaises(ValueError):
            discovery_decision([self.profile, self.profile], self.url, self.digest, False)
