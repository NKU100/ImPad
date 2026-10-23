import json
import unittest

from scripts.ci import runtime_task
from scripts import test_wechat_raw_runtime
from scripts.ci import codex_adaptation
from scripts.test_dexkit_gradle_integration import parsed_workflows
from scripts.test_runtime_validation_workflow import _workflow_if


class RuntimeRejectionTest(unittest.TestCase):
    def setUp(self):
        self.capture = test_wechat_raw_runtime.WeChatRawRuntimeReplayTest()
        self.capture.setUp()
        self.addCleanup(self.capture.tearDown)
        self.smoke = self.capture.smoke
        self.smoke['status'] = 'RUNTIME_REJECTED'
        self.smoke['reason'] = 'Module-on login page has no tablet entry'

    def reject(self):
        return runtime_task.verify_runtime_rejection(
            self.smoke, self.capture.root, self.capture.candidate, self.capture.module)

    def remove_tablet_entry(self):
        path = self.capture.app_root / 'module-on-login.xml'
        path.write_text((self.capture.app_root / 'module-off-login.xml').read_text())
        self.capture._refresh(path)
        # A missing entry means the producer never reaches the QR captures.
        self.smoke['evidence'] = [row for row in self.smoke['evidence']
                                  if 'tablet-qr-' not in row['path']]

    def test_confirmed_missing_entry_is_replayed_without_a_success_summary(self):
        self.remove_tablet_entry()
        self.smoke.pop('baselineEvidence', None)
        self.smoke.pop('enabled', None)
        files = self.reject()
        self.assertIn('apps/wechat/module-on-login.xml', files)

    def test_rejection_claim_with_passing_raw_behavior_cannot_trigger_adaptation(self):
        with self.assertRaises(ValueError):
            self.reject()

    def test_environment_and_tampered_evidence_cannot_trigger_adaptation(self):
        for fault in ('hash', 'scope', 'installed', 'wrong_activity', 'missing_hook', 'missing_file'):
            with self.subTest(fault=fault):
                self.capture.smoke = self.capture._write_valid_capture()
                self.smoke = self.capture.smoke
                self.smoke['status'] = 'RUNTIME_REJECTED'
                self.remove_tablet_entry()
                if fault == 'missing_file':
                    self.smoke['evidence'] = self.smoke['evidence'][1:]
                else:
                    name = {'hash': 'module-on-login.xml', 'scope': 'manager-state-enabled.json',
                            'installed': 'installed-identities.json',
                            'wrong_activity': 'module-on-login-activity.txt',
                            'missing_hook': 'logcat.txt'}[fault]
                    path = self.capture.app_root / name
                    if fault == 'scope':
                        raw = json.loads(path.read_text()); raw['scopeRows'] = []
                        path.write_text(json.dumps(raw))
                    elif fault == 'installed':
                        raw = json.loads(path.read_text()); raw['module']['baseApkSha256'] = 'f' * 64
                        path.write_text(json.dumps(raw))
                    else:
                        path.write_text('invalid evidence')
                    if fault != 'hash':
                        self.capture._refresh(path)
                with self.assertRaises(ValueError):
                    self.reject()

    def test_runtime_result_exports_exact_failure_inputs_only_for_adapt(self):
        self.remove_tablet_entry()
        directory = self.capture.root
        candidate = dict(self.capture.candidate, status='PREPARED')
        candidate_path = directory / 'candidate-report.json'
        candidate_path.write_text(json.dumps(candidate))
        report_path = directory / 'runtime-candidate-report.json'
        report = {'outcome': 'RUNTIME_INCOMPATIBLE', 'candidate': candidate,
                  'diagnostics': ['Raw captures prove the entry is missing'],
                  'runtime': self.smoke}
        report_path.write_text(json.dumps(report))
        # In production the evidence lives under the downloaded runtime artifact.
        for operation, wanted in [('verify', False), ('adapt', True)]:
            with self.subTest(operation=operation):
                candidate_path.write_text(json.dumps(candidate))
                ready = runtime_task.export_runtime_adaptation(
                    report_path, candidate_path, directory, operation)
                self.assertIs(ready, wanted)
                actual = json.loads(candidate_path.read_text())
                self.assertEqual('INCOMPATIBLE' if wanted else 'PREPARED', actual['status'])
                if wanted:
                    self.assertTrue(actual['runtimeFailure']['evidence'])
                    copied = directory / 'agent-analysis'
                    codex_adaptation.copy_runtime_failure(actual, directory, copied)
                    self.assertTrue((copied / 'runtime-candidate-report.json').is_file())
                    self.assertTrue((copied / 'apps/wechat/module-on-login.xml').is_file())
        for outcome in ('ERROR', 'UNAVAILABLE', 'REGRESSION_FAILED', 'CANCELLED', 'VERIFIED'):
            report['outcome'] = outcome
            report_path.write_text(json.dumps(report))
            self.assertFalse(runtime_task.export_runtime_adaptation(
                report_path, candidate_path, directory, 'adapt'))

    def test_dynamic_failure_routes_to_adaptation_but_other_failures_do_not(self):
        workflows = parsed_workflows()
        workflow = workflows['compatibility-app.yml']
        probe = next(step for step in workflows['runtime-validation.yml']['jobs']['smoke']['steps']
                     if step.get('id') == 'probe')
        for operation in ('adapt', 'verify'):
            self.assertEqual(operation == 'adapt', _workflow_if(
                probe['continue-on-error'], {'inputs.operation': operation}))
        expression = workflow['jobs']['adapt']['if'].replace('!cancelled()', 'True')
        for outcome, expected in [('RUNTIME_INCOMPATIBLE', True), ('ERROR', False),
                                  ('UNAVAILABLE', False), ('REGRESSION_FAILED', False),
                                  ('CANCELLED', False), ('VERIFIED', False)]:
            for operation in ('adapt', 'verify'):
                with self.subTest(outcome=outcome, operation=operation):
                    context = {'github.ref': 'refs/heads/main', 'inputs.operation': operation,
                               'needs.prepare.result': 'success',
                               'needs.rules-analysis.result': 'success',
                               'needs.rules-analysis.outputs.candidate_status': 'PREPARED',
                               'needs.rules-analysis.outputs.validation_outcome': outcome,
                               'needs.prepare.outputs.pending_adaptation': 'false'}
                    self.assertEqual(expected and operation == 'adapt', _workflow_if(expression, context))
        context.update({'inputs.operation': 'adapt', 'needs.rules-analysis.result': 'failure',
                        'needs.rules-analysis.outputs.candidate_status': 'INCOMPATIBLE'})
        self.assertFalse(_workflow_if(expression, context))

    def test_runtime_failure_copy_rejects_tampering_and_path_escape(self):
        self.remove_tablet_entry()
        path = self.capture.app_root / 'module-on-login.xml'
        relative = path.relative_to(self.capture.root).as_posix()
        import hashlib
        report = {'runtimeFailure': {'outcome': 'RUNTIME_INCOMPATIBLE', 'evidence': [
            {'path': relative, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}}
        path.write_text('changed')
        with self.assertRaises(ValueError):
            codex_adaptation.copy_runtime_failure(report, self.capture.root, self.capture.root / 'analysis')
        report['runtimeFailure']['evidence'][0]['path'] = '../outside'
        with self.assertRaises(ValueError):
            codex_adaptation.copy_runtime_failure(report, self.capture.root, self.capture.root / 'analysis')


if __name__ == '__main__':
    unittest.main()
