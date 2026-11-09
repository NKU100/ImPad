import unittest
from pathlib import Path
from scripts.test_dexkit_gradle_integration import parsed_workflows
from scripts.test_runtime_validation_workflow import _workflow_if

ROOT = Path(__file__).resolve().parents[1]


class AcceptanceReuseWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / '.github/workflows/runtime-validation.yml').read_text()

    def test_only_scheduled_official_checks_can_reuse_acceptance(self):
        reuse = self.workflow.split('id: acceptance-reuse\n')[1].split('      - ', 1)[0]
        for guard in ("github.event_name == 'schedule'", "inputs.apk_url == ''", "inputs.module_run_id == ''", "inputs.candidate_mode == 'latest'"):
            self.assertIn(guard, reuse)
        self.assertGreater(self.workflow.index('id: acceptance-reuse'), self.workflow.index('name: Run fixed-record static regression'))

    def test_reuse_skips_build_runtime_and_pure_verification(self):
        build = self.workflow.split('id: module-build\n')[1].split('        run:', 1)[0]
        self.assertIn("steps.acceptance-reuse.outputs.acceptance_reused != 'true'", build)
        for job in ('smoke', 'result'):
            job_header = self.workflow.split(f'\n  {job}:\n')[1].split('    runs-on:', 1)[0]
            self.assertIn("needs.prepare.outputs.acceptance_reused != 'true'", job_header)

    def test_reused_success_is_exported_without_fake_runtime_report(self):
        outputs = self.workflow.split('\n    outputs:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertIn('acceptance_reused:', outputs)
        self.assertIn('jobs.prepare.outputs.reuse_outcome', outputs)
        self.assertIn('steps.acceptance-reuse.outputs.acceptance_reused', self.workflow)

    def test_schedule_and_manual_routing_use_actual_workflow_conditions(self):
        workflow = parsed_workflows()['runtime-validation.yml']
        step = next(row for row in workflow['jobs']['prepare']['steps'] if row.get('id') == 'acceptance-reuse')
        context = {'inputs.allow_acceptance_reuse': True, 'github.event_name': 'schedule',
                   'github.ref': 'refs/heads/main', 'github.actor': 'NKU100',
                   'github.repository_owner': 'NKU100', 'inputs.apk_url': '',
                   'inputs.module_run_id': '', 'inputs.candidate_artifact': '',
                   'inputs.candidate_mode': 'latest'}
        self.assertTrue(_workflow_if(step['if'], context))
        for key, value in [('github.event_name', 'workflow_dispatch'),
                           ('inputs.allow_acceptance_reuse', False),
                           ('github.ref', 'refs/heads/topic'), ('github.actor', 'contributor'),
                           ('inputs.apk_url', 'https://example.com/app.apk'),
                           ('inputs.module_run_id', '123'), ('inputs.candidate_artifact', 'candidate'),
                           ('inputs.candidate_mode', 'artifact')]:
            with self.subTest(key=key):
                self.assertFalse(_workflow_if(step['if'], context | {key: value}))
        for job in ('smoke', 'result'):
            condition = workflow['jobs'][job]['if']
            base = {'needs.prepare.result': 'success', 'needs.prepare.outputs.candidate_status': 'PREPARED'}
            self.assertTrue(_workflow_if(condition, base | {'needs.prepare.outputs.acceptance_reused': 'false'}))
            self.assertFalse(_workflow_if(condition, base | {'needs.prepare.outputs.acceptance_reused': 'true'}))

    def test_pending_adaptation_disables_reuse_and_preserves_runtime_artifacts(self):
        workflows = parsed_workflows()
        call = workflows['compatibility-app.yml']['jobs']['rules-analysis']
        expression = call['with']['allow_acceptance_reuse'].removeprefix('${{').removesuffix('}}').strip()
        step = next(row for row in workflows['runtime-validation.yml']['jobs']['prepare']['steps']
                    if row.get('id') == 'acceptance-reuse')
        context = {'github.event_name': 'schedule', 'github.ref': 'refs/heads/main',
                   'github.actor': 'NKU100', 'github.repository_owner': 'NKU100',
                   'inputs.apk_url': '', 'inputs.module_run_id': '',
                   'inputs.candidate_artifact': '', 'inputs.candidate_mode': 'latest',
                   'inputs.operation': 'adapt'}
        for pending, expected in [('true', False), ('false', True), ('', True)]:
            with self.subTest(pending=pending):
                allowed = _workflow_if(expression, {'needs.prepare.outputs.pending_adaptation': pending})
                self.assertEqual(expected, _workflow_if(step['if'], context | {'inputs.allow_acceptance_reuse': allowed}))
