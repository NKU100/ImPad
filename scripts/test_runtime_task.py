import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.ci.runtime_task import main, runtime_result, verify_cli, register_cli


class RuntimeTaskTest(unittest.TestCase):
    def setUp(self):
        self.identity = {'packageName': 'com.tencent.mm', 'versionName': '8.0.69',
                         'versionCode': 3040, 'abi': 'arm64-v8a',
                         'apkSha256': 'a' * 64,
                         'signerSha256': '0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c'}
        self.report = {'schemaVersion': 1, 'appId': 'wechat',
                       'status': 'STATIC_VERIFIED_PENDING_RUNTIME', 'identity': self.identity,
                       'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/weixin8069android3040_arm64.apk',
                       'expectedPlan': {'selection': 'UNIQUE', 'ruleId': 'wechat_tablet_login_001',
                                        'strategyId': 'wechat_tablet_login_behavior_001',
                                        'targets': {'tablet': {}, 'login': {}}}}
        self.smoke = {'appId': 'wechat', 'packageName': self.identity['packageName'],
                      'abi': self.identity['abi'], 'status': 'RUNTIME_SMOKE_VERIFIED',
                      'baseline': 'NO_TABLET_ENTRY', 'hookIds': ['tablet', 'login'],
                      'qrPage': 'LoginAsExDeviceUI', 'apkSha256': self.identity['apkSha256']}

    def test_matching_exact_runtime_evidence_changes_only_runtime_status(self):
        original = copy.deepcopy(self.report)
        result = runtime_result(self.report, self.smoke, 'success')
        self.assertEqual('RUNTIME_VERIFIED', result['status'])
        self.assertEqual([], result['blockers'])
        self.assertEqual(self.report, original)
        self.assertNotIn('suggestedProfile', result)

    def test_mismatched_app_identity_behavior_or_apk_is_rejected(self):
        cases = [
            ({'appId': 'qq'}, 'appId'),
            ({'packageName': 'org.example.other'}, 'packageName'),
            ({'abi': 'x86_64'}, 'abi'),
            ({'hookIds': ['tablet']}, 'hookIds'),
            ({'qrPage': 'SplashActivity'}, 'qrPage'),
            ({'apkSha256': 'b' * 64}, 'apkSha256'),
        ]
        for fields, changed in cases:
            with self.subTest(changed=changed):
                result = runtime_result(self.report, dict(self.smoke, **fields), 'success')
                self.assertEqual('RUNTIME_REJECTED', result['status'])

    def test_cancelled_or_skipped_runtime_remains_pending(self):
        for conclusion in ('cancelled', 'skipped'):
            result = runtime_result(self.report, self.smoke, conclusion)
            self.assertEqual('STATIC_VERIFIED_PENDING_RUNTIME', result['status'])
            self.assertEqual(conclusion, result['runtimeConclusion'])
            self.assertTrue(result['blockers'])

    def test_non_static_candidate_cannot_enter_runtime_acceptance(self):
        report = copy.deepcopy(self.report)
        report['status'] = 'INCOMPATIBLE'
        with self.assertRaisesRegex(ValueError, 'static verification'):
            runtime_result(report, self.smoke, 'success')

    def test_explicit_app_id_cannot_override_candidate_application(self):
        with self.assertRaises(ValueError):
            runtime_result(self.report, self.smoke, 'success', 'qq')

    def test_runtime_result_command_writes_report_without_acceptance_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'candidate-report.json').write_text(json.dumps(self.report))
            smoke_path = root / 'smoke.json'
            smoke_path.write_text(json.dumps(self.smoke))
            with patch('sys.argv', ['runtime_task', '--app-id', 'wechat', '--directory', str(root),
                                    '--smoke', str(smoke_path), '--conclusion', 'success']), \
                 patch('scripts.ci.runtime_task.subprocess') as subprocess:
                main()
            subprocess.assert_not_called()
            result = json.loads((root / 'runtime-result.json').read_text())
            self.assertEqual('RUNTIME_VERIFIED', result['status'])
            self.assertFalse((root / 'acceptance.json').exists())
            self.assertFalse((root / 'targets.json').exists())

    def test_verify_and_register_clis_do_not_expose_initial_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = ['--app-id', 'wechat', '--candidate', str(root / 'candidate.json'),
                      '--module-apk', str(root / 'module.apk'), '--regression', str(root / 'regression.json'),
                      '--smoke', str(root / 'smoke.json'), '--evidence-root', str(root),
                      '--acceptance', str(root / 'acceptance.json'), '--repository', str(root),
                      '--conclusion', 'success']
            with patch('scripts.ci.runtime_task.verify_validation_files') as verifier, \
                 self.assertRaises(SystemExit):
                verify_cli(common + ['--initial-migration', '--output', str(root / 'verify.json')])
            verifier.assert_not_called()
            register_args = common + ['--expected-head', 'a' * 40, '--initial-migration',
                                      '--output', str(root / 'register.json')]
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'owner', 'GITHUB_REPOSITORY_OWNER': 'owner'}), \
                 patch('scripts.ci.runtime_task.verify_validation_files') as verifier, \
                 self.assertRaises(SystemExit):
                register_cli(register_args)
            verifier.assert_not_called()
            self.assertFalse((root / 'acceptance.json').exists())
            self.assertFalse((root / 'register.json').exists())


if __name__ == '__main__':
    unittest.main()
