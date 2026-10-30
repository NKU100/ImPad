import unittest
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from scripts.test_dexkit_gradle_integration import parsed_workflows


class CompatibilityBadgeWorkflowTest(unittest.TestCase):
    def test_scheduled_adaptation_keeps_compatible_result_when_no_change_is_needed(self):
        from scripts.ci.compatibility_badge import classify
        from scripts.test_compatibility_badge import run
        report = self._run_terminal_report_step({
            'OPERATION': 'adapt', 'RULES_OUTCOME': 'VERIFIED',
            'RULES_CANDIDATE_STATUS': 'PREPARED', 'RULES_VERSION_NAME': '8.0.60',
            'RULES_VERSION_CODE': '806000', 'RULES_CANDIDATE_SHA256': 'b' * 64,
            'RULES_REGRESSION_OUTCOME': 'PASSED', 'RULES_CHECK_OUTCOME': 'PASSED',
            'RULES_BUILD_OUTCOME': 'PASSED', 'RULES_RUNTIME_ENVIRONMENT': 'HEALTHY',
            'ADAPT_RESULT': 'skipped', 'PENDING_ADAPTATION': 'false',
        })
        self.assertEqual(('green', '已兼容'),
                         (classify(run(), report)['state'], classify(run(), report)['message']))

    def _run_terminal_report_step(self, env):
        workflow = parsed_workflows()['compatibility-app.yml']
        badge_job = workflow['jobs']['compatibility-badge']
        step = next(item for item in badge_job['steps']
                    if item.get('name') == 'Write compatibility badge report')
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_env = dict(
                os.environ, PATH=os.environ['PATH'], PYTHONPATH=str(repository),
                APP_ID=env.get('APP_ID', 'wechat'),
                OPERATION=env.get('OPERATION', 'verify'),
                GITHUB_RUN_ID='120', GITHUB_RUN_NUMBER='40', GITHUB_RUN_ATTEMPT='1',
                GITHUB_SHA='a' * 40, GITHUB_REPOSITORY='NKU100/ImPad',
                GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF_NAME='main',
            )
            for name in badge_job['env']:
                if name in env:
                    runtime_env[name] = env[name]
            result = subprocess.run(['bash', '-e', '-c', step['run']], cwd=root,
                                    env=runtime_env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            return json.loads((root / 'work/compatibility-badge/compatibility-badge.json').read_text())

    def _run_adaptation_badge_step(self, identity, claimed='true', model_outcome='',
                                   needs_model='false', static_outcome='success',
                                   task_exists=True, prepared_identity=None,
                                   app_id='wechat'):
        workflow = parsed_workflows()['compatibility-app.yml']
        job = workflow['jobs']['adapt']
        step = next(item for item in job['steps']
                    if item.get('name') == 'Export allowlisted adaptation badge evidence')
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / 'impad-adaptation'
            worktree = task / 'checkout'
            report = {'schemaVersion': 2, 'appId': 'wechat', 'status': 'INCOMPATIBLE',
                      'identity': identity}
            if task_exists:
                (task / 'state.json').parent.mkdir(parents=True)
                (task / 'state.json').write_text(json.dumps({
                    'appId': 'wechat', 'report': report, 'worktree': str(worktree),
                }))
                prepared_path = worktree / 'work/analysis/candidate-report.json'
                prepared_path.parent.mkdir(parents=True)
                prepared_path.write_text(json.dumps({
                    'schemaVersion': 2, 'appId': 'wechat', 'status': 'PREPARED',
                    'identity': prepared_identity or identity,
                }))
            output = root / 'github-output'
            env = dict(os.environ, PYTHONPATH=str(repository), APP_ID=app_id,
                       CODEX_TASK_DIR=str(task), CLAIMED=claimed,
                       MODEL_OUTCOME=model_outcome, NEEDS_MODEL=needs_model,
                       STATIC_VALIDATION_OUTCOME=static_outcome,
                       GITHUB_OUTPUT=str(output))
            result = subprocess.run(['bash', '-e', '-c', step['run']], cwd=root,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            return dict(line.split('=', 1) for line in output.read_text().splitlines())

    def test_actual_verify_workflow_script_binds_candidate_outputs_into_green_report(self):
        badge_env = parsed_workflows()['compatibility-app.yml']['jobs']['compatibility-badge']['env']
        expected_outputs = {
            'VERIFY_CANDIDATE_STATUS', 'VERIFY_VERSION_NAME', 'VERIFY_VERSION_CODE',
            'VERIFY_CANDIDATE_SHA256', 'VERIFY_REGRESSION_OUTCOME', 'VERIFY_CHECK_OUTCOME',
            'VERIFY_BUILD_OUTCOME', 'VERIFY_RUNTIME_ENVIRONMENT',
        }
        self.assertTrue(expected_outputs.issubset(badge_env))
        report = self._run_terminal_report_step({
            'VERIFY_OUTCOME': 'VERIFIED', 'VERIFY_CANDIDATE_STATUS': 'PREPARED',
            'VERIFY_VERSION_NAME': '8.0.60', 'VERIFY_VERSION_CODE': '806000',
            'VERIFY_CANDIDATE_SHA256': 'b' * 64, 'VERIFY_REGRESSION_OUTCOME': 'PASSED',
            'VERIFY_CHECK_OUTCOME': 'PASSED', 'VERIFY_BUILD_OUTCOME': 'PASSED',
            'VERIFY_RUNTIME_ENVIRONMENT': 'HEALTHY', 'PENDING_ADAPTATION': 'false',
            'ADAPT_RESULT': 'skipped',
        })
        self.assertEqual(('PREPARED', '8.0.60', 806000, 'b' * 64),
                         (report['candidateStatus'], report['versionName'],
                          report['versionCode'], report['candidateSha256']))
        from scripts.ci.compatibility_badge import classify
        self.assertEqual('green', classify(
            {'repository': 'NKU100/ImPad', 'headRepository': 'NKU100/ImPad',
             'workflowPath': '.github/workflows/compatibility-wechat.yml',
             'event': 'workflow_dispatch', 'branch': 'main', 'status': 'completed',
             'conclusion': 'success', 'runId': 120, 'runNumber': 40, 'runAttempt': 1,
             'sha': 'a' * 40, 'appId': 'wechat'}, report)['state'])

    def test_actual_runtime_adaptation_script_keeps_runtime_evidence_and_pr_gate(self):
        adaptation_evidence = self._run_adaptation_badge_step({
            'versionName': '8.0.61', 'versionCode': 806100, 'apkSha256': 'c' * 64,
        })
        report = self._run_terminal_report_step({
            'OPERATION': 'adapt', 'VERIFY_OUTCOME': '',
            'RUNTIME_OUTCOME': 'VERIFIED', 'RUNTIME_CANDIDATE_STATUS': 'PREPARED',
            'RUNTIME_VERSION_NAME': '8.0.61', 'RUNTIME_VERSION_CODE': '806100',
            'RUNTIME_CANDIDATE_SHA256': 'c' * 64,
            'RUNTIME_REGRESSION_OUTCOME': 'PASSED', 'RUNTIME_CHECK_OUTCOME': 'PASSED',
            'RUNTIME_BUILD_OUTCOME': 'PASSED', 'RUNTIME_ENVIRONMENT': 'HEALTHY',
            'PENDING_ADAPTATION': 'false', 'ADAPT_RESULT': 'success',
            'ADAPT_STATUS': 'WAITING_RUNTIME', 'CANDIDATE_READY': 'true',
            'RUNTIME_RESULT_STATUS': 'VERIFIED',
            **{f"ADAPT_BADGE_{key.upper()}": value
               for key, value in adaptation_evidence.items()},
        })
        self.assertEqual(('8.0.61', 'c' * 64, 'HEALTHY', 'REGISTERED', True),
                         (report['versionName'], report['candidateSha256'],
                          report['runtimeEnvironment'], report['registrationStatus'],
                          report['prReady']))
        from scripts.ci.compatibility_badge import classify
        self.assertEqual(('yellow', '待合并'),
                         (classify({'repository': 'NKU100/ImPad',
                                    'headRepository': 'NKU100/ImPad',
                                    'workflowPath': '.github/workflows/compatibility-wechat.yml',
                                    'event': 'workflow_dispatch', 'branch': 'main',
                                    'status': 'completed', 'conclusion': 'success',
                                    'runId': 120, 'runNumber': 40, 'runAttempt': 1,
                                    'sha': 'a' * 40, 'appId': 'wechat'}, report)['state'],
                          classify({'repository': 'NKU100/ImPad',
                                    'headRepository': 'NKU100/ImPad',
                                    'workflowPath': '.github/workflows/compatibility-wechat.yml',
                                    'event': 'workflow_dispatch', 'branch': 'main',
                                    'status': 'completed', 'conclusion': 'success',
                                    'runId': 120, 'runNumber': 40, 'runAttempt': 1,
                                    'sha': 'a' * 40, 'appId': 'wechat'}, report)['message']))

    def test_adaptation_environment_failure_before_claim_stays_gray(self):
        evidence = self._run_adaptation_badge_step(
            {'versionName': '8.0.60', 'versionCode': 806000, 'apkSha256': 'b' * 64},
            claimed='false', needs_model='', static_outcome='', task_exists=False)
        report = self._run_terminal_report_step({
            'OPERATION': 'adapt', 'RULES_OUTCOME': 'NO_MATCH',
            'RULES_CANDIDATE_STATUS': 'INCOMPATIBLE', 'RULES_VERSION_NAME': '8.0.60',
            'RULES_VERSION_CODE': '806000', 'RULES_CANDIDATE_SHA256': 'b' * 64,
            'RULES_REGRESSION_OUTCOME': 'PASSED', 'RULES_CHECK_OUTCOME': 'PASSED',
            'PENDING_ADAPTATION': 'false', 'ADAPT_RESULT': 'failure',
            'ADAPT_STATUS': '', 'JOB_STATUS': 'failure',
            **{f"ADAPT_BADGE_{key.upper()}": value for key, value in evidence.items()},
        })
        from scripts.ci.compatibility_badge import classify
        result = classify({
            'repository': 'NKU100/ImPad', 'headRepository': 'NKU100/ImPad',
            'workflowPath': '.github/workflows/compatibility-wechat.yml',
            'event': 'workflow_dispatch', 'branch': 'main', 'status': 'completed',
            'conclusion': 'failure', 'runId': 120, 'runNumber': 40, 'runAttempt': 1,
            'sha': 'a' * 40, 'appId': 'wechat',
        }, report)
        self.assertEqual(('ERROR', False, 'gray', '执行异常'),
                         (report['outcome'], report['adaptationAttempted'],
                          result['state'], result['message']))

    def test_adaptation_static_summary_uses_task_candidate_for_waiting_runtime(self):
        evidence = self._run_adaptation_badge_step({
            'versionName': '8.0.61', 'versionCode': 806100, 'apkSha256': 'c' * 64,
        })
        report = self._run_terminal_report_step({
            'OPERATION': 'adapt', 'RULES_OUTCOME': 'NO_MATCH',
            'RULES_CANDIDATE_STATUS': 'INCOMPATIBLE', 'RULES_VERSION_NAME': '8.0.99',
            'RULES_VERSION_CODE': '809900', 'RULES_CANDIDATE_SHA256': 'd' * 64,
            'RULES_REGRESSION_OUTCOME': 'PASSED', 'RULES_CHECK_OUTCOME': 'PASSED',
            'PENDING_ADAPTATION': 'true', 'ADAPT_RESULT': 'success',
            'ADAPT_STATUS': 'WAITING_RUNTIME', 'CANDIDATE_READY': 'true',
            'RUNTIME_JOB_RESULT': '', 'RUNTIME_RESULT_STATUS': '',
            **{f"ADAPT_BADGE_{key.upper()}": value for key, value in evidence.items()},
        })
        from scripts.ci.compatibility_badge import classify
        result = classify({
            'repository': 'NKU100/ImPad', 'headRepository': 'NKU100/ImPad',
            'workflowPath': '.github/workflows/compatibility-wechat.yml',
            'event': 'workflow_dispatch', 'branch': 'main', 'status': 'completed',
            'conclusion': 'success', 'runId': 120, 'runNumber': 40, 'runAttempt': 1,
            'sha': 'a' * 40, 'appId': 'wechat',
        }, report)
        self.assertEqual(('PENDING_RUNTIME', 'PENDING_RUNTIME', '8.0.61', 806100,
                          'c' * 64, 'yellow', '待验证'),
                         (report['outcome'], report['validationOutcome'],
                          report['versionName'], report['versionCode'], report['candidateSha256'],
                          result['state'], result['message']))

    def test_static_adaptation_with_runtime_execution_failure_is_gray(self):
        evidence = self._run_adaptation_badge_step({
            'versionName': '8.0.61', 'versionCode': 806100, 'apkSha256': 'c' * 64,
        })
        report = self._run_terminal_report_step({
            'OPERATION': 'adapt', 'RULES_OUTCOME': 'NO_MATCH',
            'PENDING_ADAPTATION': 'false', 'ADAPT_RESULT': 'success',
            'ADAPT_STATUS': 'WAITING_RUNTIME', 'CANDIDATE_READY': 'true',
            'RUNTIME_JOB_RESULT': 'failure', 'RUNTIME_OUTCOME': 'ERROR',
            'RUNTIME_RESULT_STATUS': 'UNAVAILABLE',
            **{f"ADAPT_BADGE_{key.upper()}": value for key, value in evidence.items()},
        })
        from scripts.ci.compatibility_badge import classify
        result = classify({
            'repository': 'NKU100/ImPad', 'headRepository': 'NKU100/ImPad',
            'workflowPath': '.github/workflows/compatibility-wechat.yml',
            'event': 'workflow_dispatch', 'branch': 'main', 'status': 'completed',
            'conclusion': 'failure', 'runId': 120, 'runNumber': 40, 'runAttempt': 1,
            'sha': 'a' * 40, 'appId': 'wechat',
        }, report)
        self.assertEqual(('ERROR', 'gray', '执行异常'),
                         (report['outcome'], result['state'], result['message']))

    def test_incomplete_adaptation_without_task_identity_does_not_reuse_rules_candidate(self):
        report = self._run_terminal_report_step({
            'OPERATION': 'adapt', 'RULES_OUTCOME': 'NO_MATCH',
            'RULES_CANDIDATE_STATUS': 'INCOMPATIBLE', 'RULES_VERSION_NAME': '8.0.99',
            'RULES_VERSION_CODE': '809900', 'RULES_CANDIDATE_SHA256': 'd' * 64,
            'PENDING_ADAPTATION': 'false', 'ADAPT_STATUS': 'INTERRUPTED',
            'ADAPT_BADGE_ATTEMPTED': 'true', 'JOB_STATUS': 'failure',
        })
        from scripts.ci.compatibility_badge import classify
        result = classify({
            'repository': 'NKU100/ImPad', 'headRepository': 'NKU100/ImPad',
            'workflowPath': '.github/workflows/compatibility-wechat.yml',
            'event': 'workflow_dispatch', 'branch': 'main', 'status': 'completed',
            'conclusion': 'failure', 'runId': 120, 'runNumber': 40, 'runAttempt': 1,
            'sha': 'a' * 40, 'appId': 'wechat',
        }, report)
        self.assertEqual((None, None, 'orange', '适配未完成'),
                         (report['versionName'], report['candidateSha256'],
                          result['state'], result['message']))

    def test_real_adaptation_summary_step_exports_pinned_candidate_and_static_proof(self):
        workflow = parsed_workflows()['compatibility-app.yml']
        job = workflow['jobs']['adapt']
        expected_outputs = {
            'badge_attempted', 'badge_static_verified', 'badge_candidate_status',
            'badge_version_name', 'badge_version_code', 'badge_candidate_sha256',
            'badge_regression_outcome', 'badge_check_outcome', 'badge_build_outcome',
        }
        self.assertEqual(expected_outputs, set(job['outputs']) & expected_outputs)
        step = next(item for item in job['steps']
                    if item.get('name') == 'Export allowlisted adaptation badge evidence')
        self.assertEqual('always()', step['if'])
        self.assertIn('compatibility_badge adaptation-summary', step['run'])
        self.assertIn('--app-id "$APP_ID"', step['run'])
        identity = {'versionName': '8.0.61', 'versionCode': 806100, 'apkSha256': 'c' * 64}
        evidence = self._run_adaptation_badge_step(identity)
        self.assertEqual({
            'attempted': 'true', 'static_verified': 'true', 'candidate_status': 'PREPARED',
            'version_name': '8.0.61', 'version_code': '806100', 'candidate_sha256': 'c' * 64,
            'regression_outcome': 'PASSED', 'check_outcome': 'PASSED', 'build_outcome': 'PASSED',
        }, evidence)

    def test_adaptation_summary_rejects_prepared_report_for_a_different_candidate(self):
        evidence = self._run_adaptation_badge_step(
            {'versionName': '8.0.60', 'versionCode': 806000, 'apkSha256': 'b' * 64},
            model_outcome='failure', needs_model='true', static_outcome='failure',
            prepared_identity={
                'versionName': '8.0.61', 'versionCode': 806100, 'apkSha256': 'c' * 64,
            })
        self.assertEqual({
            'attempted': 'true', 'static_verified': 'false', 'candidate_status': 'INCOMPATIBLE',
            'version_name': '8.0.60', 'version_code': '806000', 'candidate_sha256': 'b' * 64,
            'regression_outcome': 'UNKNOWN', 'check_outcome': 'UNKNOWN',
            'build_outcome': 'UNKNOWN',
        }, evidence)

    def test_adaptation_summary_rejects_task_evidence_for_another_application(self):
        evidence = self._run_adaptation_badge_step(
            {'versionName': '8.0.60', 'versionCode': 806000, 'apkSha256': 'b' * 64},
            app_id='qq')
        self.assertEqual('false', evidence['static_verified'])
        self.assertEqual('false', evidence['attempted'])
        self.assertEqual('UNAVAILABLE', evidence['candidate_status'])
        self.assertEqual('', evidence['candidate_sha256'])

    def test_runtime_validation_exports_only_sanitized_badge_outputs(self):
        workflow = parsed_workflows()['runtime-validation.yml']
        outputs = workflow['true']['workflow_call']['outputs']
        expected = {
            'badge_outcome', 'badge_candidate_status', 'badge_version_name',
            'badge_version_code', 'badge_candidate_sha256', 'badge_regression_outcome',
            'badge_check_outcome', 'badge_build_outcome', 'badge_runtime_environment',
        }
        self.assertEqual(expected, set(outputs) - {'candidate_status', 'candidate_artifact',
                                                   'validation_outcome'})
        result = workflow['jobs']['result']
        self.assertEqual(expected, set(result['outputs']) - {'validation_outcome',
                                                             'adaptation_artifact'})
        verdict = next(step for step in result['steps'] if step.get('id') == 'verdict')
        self.assertIn('write_runtime_summary', verdict['run'])

    def test_prepare_failure_evidence_uses_structured_gradle_report(self):
        workflow = parsed_workflows()['runtime-validation.yml']
        prepare = workflow['jobs']['prepare']
        self.assertIn('badge_build_outcome', prepare['outputs'])
        self.assertIn('badge_check_outcome', prepare['outputs'])
        build = next(step for step in prepare['steps'] if step.get('id') == 'module-build')
        self.assertIn('gradle_failure_evidence.init.gradle', build['run'])
        exporter = next(step for step in prepare['steps']
                        if step.get('name') == 'Export preparation badge evidence')
        self.assertEqual('always()', exporter['if'])
        self.assertIn('gradle-failure', exporter['run'])

    def test_adaptation_policy_tests_export_machine_readable_failure_evidence(self):
        workflow = parsed_workflows()['compatibility-app.yml']
        prepare = workflow['jobs']['prepare']
        self.assertIn('badge_check_outcome', prepare['outputs'])
        tests = next(step for step in prepare['steps']
                     if step.get('name') == 'Test app discovery, shared candidate preparation, and policy isolation')
        self.assertEqual('contract-tests', tests['id'])
        self.assertIn('scripts.ci.run_unit_tests', tests['run'])
        self.assertIn('--tests', tests['run'])
        reporter = next(step for step in prepare['steps']
                        if step.get('name') == 'Export compatibility test outcome')
        self.assertEqual('always()', reporter['if'])
        self.assertIn('unit_tests_outcome', reporter['run'])

    def test_shared_compatibility_workflow_always_writes_one_terminal_report(self):
        workflow = parsed_workflows()['compatibility-app.yml']
        summary = workflow['jobs']['compatibility-badge']
        self.assertIn('always()', summary['if'])
        self.assertEqual({'prepare', 'verify', 'rules-analysis', 'adapt', 'runtime', 'runtime-result'},
                         set(summary['needs']))
        steps = summary['steps']
        write = next(step for step in steps if step.get('name') == 'Write compatibility badge report')
        self.assertIn('scripts.ci.compatibility_badge write', write['run'])
        self.assertEqual('${{ needs.adapt.outputs.badge_attempted }}',
                         summary['env']['ADAPT_BADGE_ATTEMPTED'])
        self.assertEqual('${{ needs.runtime.result }}', summary['env']['RUNTIME_JOB_RESULT'])
        upload = next(step for step in steps if step.get('uses') == 'actions/upload-artifact@v7')
        self.assertEqual('always()', upload['if'])
        self.assertIn('compatibility-badge.json', upload['with']['path'])

    def test_publisher_accepts_only_completed_official_main_app_entrypoints(self):
        workflow = parsed_workflows()['publish-compatibility-badge.yml']
        triggers = workflow['true']
        self.assertEqual(['completed'], triggers['workflow_run']['types'])
        self.assertEqual({'Check WeChat compatibility', 'Check QQ compatibility',
                          'Check WeCom compatibility'}, set(triggers['workflow_run']['workflows']))
        condition = workflow['jobs']['publish']['if']
        for required in ('NKU100/ImPad', 'workflow_dispatch', 'schedule', "head_branch == 'main'",
                         'head_repository.full_name'):
            self.assertIn(required, condition)
        self.assertIn('scripts.ci.compatibility_badge publish',
                      next(step for step in workflow['jobs']['publish']['steps']
                           if 'Publish terminal' in step.get('name', ''))['run'])

    def test_publisher_serializes_updates_per_app_while_cas_shares_the_data_branch(self):
        workflow = parsed_workflows()['publish-compatibility-badge.yml']
        concurrency = workflow['concurrency']
        self.assertEqual(
            'impad-compatibility-badge-${{ github.event.workflow_run.path }}',
            concurrency['group'])
        self.assertFalse(concurrency['cancel-in-progress'])
        self.assertEqual('compatibility-badge-data',
                         __import__('scripts.ci.compatibility_badge',
                                    fromlist=['BRANCH']).BRANCH)


    def test_readmes_use_three_json_endpoints_without_changing_build_badge(self):
        from pathlib import Path

        from urllib.parse import unquote

        readme = unquote(Path('README.md').read_text())
        self.assertIn('compatibility-badge-data/wechat.json', readme)
        self.assertIn('compatibility-badge-data/qq.json', readme)
        self.assertIn('compatibility-badge-data/wecom.json', readme)
        self.assertIn('build-badge-data/build.json', readme)
        for old in ('github/actions/workflow/status/NKU100/ImPad/compatibility-wechat.yml',
                    'github/actions/workflow/status/NKU100/ImPad/compatibility-qq.yml',
                    'github/actions/workflow/status/NKU100/ImPad/compatibility-wecom.yml'):
            self.assertNotIn(old, readme)


if __name__ == '__main__':
    unittest.main()
