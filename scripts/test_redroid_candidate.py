import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from scripts.ci import redroid_prepare_apps as prepare
from scripts.ci import redroid_app_smoke as runtime
from scripts.ci.runtime_task import runtime_result

class CandidateRuntimeTest(unittest.TestCase):
    def candidate(self, app):
        profile = json.loads(Path('environment/redroid-smoke/' + app + '.json').read_text())[-1]
        profile['verificationStatus'] = 'static-verified'
        return {'appId': app, 'status': 'STATIC_VERIFIED_PENDING_RUNTIME', 'identity': profile['identity'],
                'sourceUrl': profile['sourceUrl'], 'suggestedProfile': profile}

    def test_pending_candidate_is_staged_without_promoting_profile(self):
        report = self.candidate('qq')
        original = copy.deepcopy(report)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = prepare.write_candidate(report, root)
            self.assertEqual(set(inputs), {'qq'})
            profile = json.loads((root/'assets/compatibility-local/qq/targets.json').read_text())[0]
            self.assertEqual(profile['verificationStatus'], 'static-verified')
            self.assertIsNone(profile.get('sourceUrl'))
            self.assertEqual(report, original)

    def test_rejects_candidate_with_different_profile_identity(self):
        report = self.candidate('qq')
        report['suggestedProfile']['identity'] = dict(report['identity'], apkSha256='a'*64)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                prepare.write_candidate(report, Path(directory))

    def test_setup_failure_preserves_standard_candidate_report(self):
        candidate = self.candidate('qq')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'inputs.json').write_text(json.dumps({'qq': candidate['suggestedProfile']}))
            (root/'candidate-report.json').write_text(json.dumps(candidate))
            (root/'module').mkdir()
            (root/'module/app-debug.apk').write_bytes(b'module')
            app = runtime.AppSmoke(root, MagicMock(output=root))
            with patch.object(app, 'setup', side_effect=RuntimeError('Framework unavailable')), patch.object(app, 'monitor'):
                with self.assertRaisesRegex(RuntimeError, 'Framework unavailable'):
                    app.execute()
            report = json.loads((root/'evidence/smoke-report.json').read_text())
            self.assertEqual(report['status'], 'ACCEPTANCE_UNAVAILABLE')
            self.assertEqual(report['apkSha256'], candidate['identity']['apkSha256'])
            self.assertIn('Framework unavailable', report['reason'])

    def test_ui_report_matches_pipeline_acceptance_contract(self):
        report = self.candidate('wechat')
        result = runtime.pipeline_report({'appId': 'wechat', 'status': 'RUNTIME_SMOKE_VERIFIED',
                 'baseline': {'moduleEnabled': False, 'loginPageReady': True, 'tabletEntry': False},
                 'qrPage': 'LoginAsExDeviceUI', 'moduleApkSha256': 'b'*64}, report)
        self.assertEqual(runtime_result(report, result, 'success')['status'], 'RUNTIME_VERIFIED')
        self.assertEqual(result['baselineEvidence']['moduleEnabled'], False)
