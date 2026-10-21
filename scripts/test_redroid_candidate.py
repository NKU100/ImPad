import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.ci import redroid_app_smoke as runtime
from scripts.ci import redroid_prepare_apps as prepare
from scripts.ci.runtime_task import runtime_result


class CandidateRuntimeTest(unittest.TestCase):
    def candidate(self, app='wechat', status='PREPARED'):
        identities = {
            'wechat': ('com.tencent.mm', '0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c'),
            'qq': ('com.tencent.mobileqq', 'ea6e97ad6c34f7039a9c6daba732c97d0e098e83ede2b4d52c76eb0184ac7a38'),
            'wecom': ('com.tencent.wework', '66896ae0aae48b9e963a3e034fa2ca813ca2f661f53619a92263a8a5c2e3f388'),
        }
        package, signer = identities[app]
        digest = 'a' * 64
        return {
            'schemaVersion': 1, 'appId': app, 'status': status,
            'identity': {'packageName': package, 'versionName': '8.0.69',
                         'versionCode': 3040, 'abi': 'arm64-v8a',
                         'apkSha256': digest, 'signerSha256': signer},
            'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/weixin8069android3040_arm64.apk',
            'apkFiles': [{'path': f'{app}.apk', 'role': 'base', 'sha256': digest}],
            'expectedPlan': {'selection': 'UNIQUE', 'ruleId': f'{app}_rule_001',
                             'strategyId': f'{app}_strategy_001', 'targets': {'target': {}}},
            'staticReport': 'static-check.json', 'staticReportSha256': 'c' * 64,
            'ruleCatalogSha256': 'd' * 64,
        }

    def test_candidate_preparation_writes_only_app_scope_not_targets_assets(self):
        report = self.candidate('wechat')
        original = copy.deepcopy(report)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = prepare.write_candidate(report, root)
            self.assertEqual({'wechat'}, set(inputs))
            self.assertEqual('com.tencent.mm\n',
                             (root / 'resources/META-INF/xposed/scope.list').read_text())
            self.assertFalse(any(root.rglob('targets.json')))
            self.assertFalse((root / 'assets/compatibility-local').exists())
            self.assertEqual(report, original)

    def test_rejects_candidate_whose_static_input_identity_disagrees(self):
        report = self.candidate('wechat')
        report['apkFiles'][0]['sha256'] = 'b' * 64
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'identity or unique rule plan'):
                prepare.write_candidate(report, Path(directory))

    def test_setup_failure_preserves_standard_candidate_report(self):
        candidate = self.candidate('qq')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = prepare.write_candidate(candidate, root)
            (root / 'inputs.json').write_text(json.dumps(inputs))
            (root / 'candidate-report.json').write_text(json.dumps(candidate))
            (root / 'module').mkdir()
            (root / 'module/app-debug.apk').write_bytes(b'module')
            app = runtime.AppSmoke(root, MagicMock(output=root))
            with patch.object(app, 'setup', side_effect=RuntimeError('Framework unavailable')), \
                 patch.object(app, 'monitor'):
                with self.assertRaisesRegex(RuntimeError, 'Framework unavailable'):
                    app.execute()
            report = json.loads((root / 'evidence/smoke-report.json').read_text())
            self.assertEqual(report['status'], 'ACCEPTANCE_UNAVAILABLE')
            self.assertEqual(report['apkSha256'], candidate['identity']['apkSha256'])
            self.assertIn('Framework unavailable', report['reason'])

    def test_wechat_ui_report_matches_shared_runtime_evidence_contract(self):
        report = self.candidate('wechat', 'STATIC_VERIFIED_PENDING_RUNTIME')
        with tempfile.TemporaryDirectory() as directory:
            evidence_root = Path(directory)
            evidence_directory = evidence_root / 'apps/wechat'
            evidence_directory.mkdir(parents=True)
            (evidence_directory / 'logcat.txt').write_text('captured app log')
            module_identity = {'sha256': 'b' * 64, 'sourceTreeSha256': 'c' * 64,
                               'ruleCatalogSha256': 'd' * 64, 'routeSha256': 'e' * 64,
                               'dependencyLockSha256': 'f' * 64}
            result = runtime.pipeline_report({
                'appId': 'wechat', 'status': 'RUNTIME_SMOKE_VERIFIED',
                'baseline': {'moduleEnabled': False, 'loginPageReady': True, 'tabletEntry': False},
                'qrPage': 'LoginAsExDeviceUI', 'moduleApkSha256': 'b' * 64,
                'evidenceDirectory': str(evidence_directory),
            }, report, module_identity, evidence_root)
        self.assertEqual('RUNTIME_VERIFIED', runtime_result(report, result, 'success')['status'])
        self.assertEqual(result['baselineEvidence']['moduleEnabled'], False)

    def test_only_the_official_single_signer_is_accepted(self):
        digest = 'a' * 64
        prepare.verify_signer('Signer #1 certificate SHA-256 digest: ' + digest, digest)
        prepare.verify_signer('V2 Signer: certificate SHA-256 digest: ' + digest, digest)
        prepare.verify_signer('V3.0 Signer: certificate SHA-256 digest: ' + digest, digest)
        with self.assertRaises(ValueError):
            prepare.verify_signer('Signer #1 certificate SHA-256 digest: ' + 'b' * 64, digest)
        with self.assertRaises(ValueError):
            prepare.verify_signer('Signer #1 certificate SHA-256 digest: ' + digest +
                                  '\nSigner #2 certificate SHA-256 digest: ' + digest, digest)


if __name__ == '__main__':
    unittest.main()
