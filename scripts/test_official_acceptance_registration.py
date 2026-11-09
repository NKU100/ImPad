import unittest
import importlib

from scripts.test_dexkit_gradle_integration import parsed_workflows
from scripts.test_runtime_validation_workflow import _workflow_if


class OfficialAcceptanceRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.context = {
            '!cancelled()': True,
            'github.event_name': 'schedule',
            'github.ref': 'refs/heads/main',
            'github.actor': 'NKU100',
            'github.repository_owner': 'NKU100',
            'inputs.operation': 'adapt',
            'inputs.apk_url': '',
            'inputs.module_run_id': '',
            'inputs.module_artifact': '',
            'needs.prepare.result': 'success',
            'needs.prepare.outputs.pending_adaptation': 'false',
            'needs.rules-analysis.result': 'success',
            'needs.rules-analysis.outputs.validation_outcome': 'VERIFIED',
            'needs.rules-analysis.outputs.acceptance_reused': 'false',
        }

    def test_workflow_routes_only_eligible_official_adapt_runs_to_registration(self):
        jobs = parsed_workflows()['compatibility-app.yml']['jobs']
        self.assertIn('register-official-acceptance', jobs)
        job = jobs['register-official-acceptance']
        condition = job['if']
        cases = [
            ('scheduled owner adapt', {}, True),
            ('cancelled', {'!cancelled()': False}, False),
            ('manual owner adapt', {'github.event_name': 'workflow_dispatch'}, True),
            ('manual verify', {'inputs.operation': 'verify'}, False),
            ('foreign actor', {'github.actor': 'other'}, False),
            ('non-main branch', {'github.ref': 'refs/heads/topic'}, False),
            ('explicit APK URL', {'inputs.apk_url': 'https://example.test/app.apk'}, False),
            ('explicit module run', {'inputs.module_run_id': '123'}, False),
            ('explicit module artifact', {'inputs.module_artifact': 'module.apk'}, False),
            ('failed rules analysis', {'needs.rules-analysis.result': 'failure'}, False),
            ('not verified', {'needs.rules-analysis.outputs.validation_outcome': 'INCOMPATIBLE'}, False),
            ('reused result', {'needs.rules-analysis.outputs.acceptance_reused': 'true'}, False),
            ('pending adaptation', {'needs.prepare.outputs.pending_adaptation': 'true'}, False),
        ]
        for label, changed, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(expected, _workflow_if(condition, self.context | changed))

    def test_pending_adaptation_disables_caller_permission_and_runtime_reuse_guard(self):
        workflow = parsed_workflows()['compatibility-app.yml']
        caller_permission = workflow['jobs']['rules-analysis']['with']['allow_acceptance_reuse']
        runtime = parsed_workflows()['runtime-validation.yml']
        reuse_step = next(step for step in runtime['jobs']['prepare']['steps']
                          if step.get('id') == 'acceptance-reuse')
        for pending, expected in [('false', True), ('true', False)]:
            with self.subTest(pending=pending):
                allowed = _workflow_if(
                    caller_permission,
                    {'needs.prepare.outputs.pending_adaptation': pending})
                context = self.context | {'inputs.allow_acceptance_reuse': allowed,
                    'inputs.candidate_artifact': '', 'inputs.candidate_mode': 'latest'}
                self.assertEqual(expected, allowed)
                self.assertEqual(expected, _workflow_if(reuse_step['if'], context))

    def test_helper_gate_rejects_manual_verify_and_explicit_candidate_inputs(self):
        registration = self._helper()
        values = dict(
            event_name='schedule', ref='refs/heads/main', actor='NKU100', owner='NKU100',
            operation='adapt', apk_url='', module_run_id='', module_artifact='',
            outcome='VERIFIED', reused='false', pending_adaptation='false',
        )
        self.assertTrue(registration.registration_is_eligible(**values))
        for change in (
            {'event_name': 'workflow_dispatch', 'operation': 'verify'},
            {'actor': 'other'},
            {'ref': 'refs/heads/topic'},
            {'apk_url': 'https://example.test/app.apk'},
            {'module_run_id': '123'},
            {'module_artifact': 'module.apk'},
            {'outcome': 'INCOMPATIBLE'},
            {'reused': 'true'},
            {'pending_adaptation': 'true'},
        ):
            with self.subTest(change=change):
                self.assertFalse(registration.registration_is_eligible(**(values | change)))

    def test_existing_acceptance_is_idempotent_only_for_same_apk_and_execution_identity(self):
        registration = self._helper()
        candidate = {
            'apkFiles': [{'role': 'base', 'sha256': 'a' * 64}],
            'expectedPlan': {'ruleId': 'rule-1'},
            'expectedExecutionSha256': 'c' * 64,
            'recognitionSha256': 'd' * 64,
        }
        static = {'executionIdentity': {'sourceTreeSha256': 'b' * 64}}
        record = {
            'status': 'VERIFIED',
            'apkFiles': candidate['apkFiles'],
            'completePlan': candidate['expectedPlan'],
            'executionSha256': candidate['expectedExecutionSha256'],
            'recognitionSha256': candidate['recognitionSha256'],
            'staticReport': static,
        }
        self.assertTrue(registration.acceptance_matches_candidate(record, candidate, static))

        other_execution = {'executionIdentity': {'sourceTreeSha256': 'c' * 64}}
        self.assertFalse(registration.acceptance_matches_candidate(record, candidate, other_execution))
        other_apk = {'apkFiles': [{'role': 'base', 'sha256': 'd' * 64}]}
        self.assertFalse(registration.acceptance_matches_candidate(record, other_apk, static))
        other_plan = candidate | {'expectedPlan': {'ruleId': 'rule-2'}}
        self.assertFalse(registration.acceptance_matches_candidate(record, other_plan, static))

    def _helper(self):
        try:
            return importlib.import_module('scripts.ci.register_official_acceptance')
        except ImportError as error:
            self.fail(f'official acceptance registration helper is missing: {error}')


if __name__ == '__main__':
    unittest.main()
