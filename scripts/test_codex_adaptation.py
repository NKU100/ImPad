import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.ci.acceptance import RoutingReviewRequired
from scripts.ci.codex_adaptation import (
    candidate_key, eligible_candidate, gradle_command, run_bounded,
    sha256, validate_existing_tests, validate_paths,
)


class CodexAdaptationTest(unittest.TestCase):
    def setUp(self):
        self.record = {
            'recordId': 'wechat-first', 'appId': 'wechat',
            'identity': {'packageName': 'com.tencent.mm', 'versionName': '8.0.69',
                         'versionCode': 3040, 'abi': 'arm64-v8a',
                         'apkSha256': 'b' * 64,
                         'signerSha256': '0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c'},
        }
        self.report = {
            'schemaVersion': 1, 'appId': 'wechat', 'status': 'INCOMPATIBLE',
            'identity': {'packageName': 'com.tencent.mm', 'versionName': '8.0.80',
                         'versionCode': 3220, 'abi': 'arm64-v8a',
                         'apkSha256': 'a' * 64,
                         'signerSha256': '0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c'},
            'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/weixin8080android3220_arm64.apk',
        }

    def test_forward_candidate_is_keyed_by_exact_app_identity(self):
        self.assertEqual('wechat-com.tencent.mm-arm64-v8a-3220-' + 'a' * 64,
                         candidate_key(self.report))
        self.assertTrue(eligible_candidate(self.report, [self.record]))

    def test_known_digest_is_not_readapted_and_unknown_old_build_needs_routing_review(self):
        same = copy.deepcopy(self.report)
        same['identity']['apkSha256'] = self.record['identity']['apkSha256']
        self.assertFalse(eligible_candidate(same, [self.record]))

        older = copy.deepcopy(self.report)
        older['identity']['versionCode'] = 3040
        with self.assertRaises(RoutingReviewRequired):
            eligible_candidate(older, [self.record])

    def test_identity_and_official_source_are_revalidated(self):
        for field, value in [('packageName', 'org.example.other'),
                             ('signerSha256', 'c' * 64), ('abi', 'x86_64')]:
            report = copy.deepcopy(self.report)
            report['identity'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                eligible_candidate(report, [self.record])
        report = copy.deepcopy(self.report)
        report['sourceUrl'] = 'https://example.invalid/candidate.apk'
        with self.assertRaisesRegex(ValueError, 'official Tencent CDN'):
            eligible_candidate(report, [self.record])

    def test_only_app_rule_runtime_and_focused_test_paths_are_allowed(self):
        validate_paths([
            'app/src/main/kotlin/io/github/nku100/impad/apps/wechat/WeChatAppAdapter.kt',
            'app/src/test/kotlin/io/github/nku100/impad/apps/wechat/WeChatAppAdapterTest.kt',
            'compat-rules/src/main/kotlin/io/github/nku100/impad/rules/wechat/WeChatRule.kt',
            'compat-rules/src/test/kotlin/io/github/nku100/impad/rules/wechat/WeChatRuleTest.kt',
        ], 'wechat')
        for path in ('compatibility/wechat/acceptance.json', 'compatibility/wechat/targets.json',
                     '.github/workflows/build.yml',
                     'app/src/main/kotlin/io/github/nku100/impad/apps/qq/QqAppAdapter.kt'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'outside'):
                validate_paths([path], 'wechat')

    def test_gradle_validation_build_has_no_candidate_asset_mode(self):
        command = gradle_command('wechat')
        self.assertIn(':app:testDebugUnitTest', command)
        self.assertIn(':compat-rules:test', command)
        self.assertIn(':app:verifyModuleMetadata', command)
        self.assertIn(':app:assembleDebug', command)
        self.assertFalse(any('runtimeLocalAssets' in argument for argument in command))

    def test_existing_tests_are_hash_protected(self):
        with tempfile.TemporaryDirectory() as temporary:
            test = Path(temporary) / 'ExistingTest.kt'
            test.write_text('assert rejects ambiguous rule\n')
            baseline = {'ExistingTest.kt': sha256(test)}
            validate_existing_tests(Path(temporary), baseline)
            test.write_text('assert true\n')
            with self.assertRaisesRegex(ValueError, 'regression tests'):
                validate_existing_tests(Path(temporary), baseline)
            test.unlink()
            with self.assertRaisesRegex(ValueError, 'regression tests'):
                validate_existing_tests(Path(temporary), baseline)

    def test_model_command_waits_without_an_agent_owned_timeout(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch('scripts.ci.codex_adaptation.subprocess.Popen') as launch:
            process = MagicMock()
            process.wait.return_value = 0
            launch.return_value = process
            run_bounded(['codex'], Path(temporary), {}, Path(temporary) / 'model.log')
            process.wait.assert_called_once_with(timeout=None)

    def test_explicit_command_timeout_terminates_the_process_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(TimeoutError):
                run_bounded(['python3', '-c', 'import time; time.sleep(30)'],
                            Path(temporary), {}, Path(temporary) / 'timeout.log', timeout=0.1)


if __name__ == '__main__':
    unittest.main()
