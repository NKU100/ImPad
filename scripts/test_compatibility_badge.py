import json
import unittest

from scripts.ci.compatibility_badge import (
    classify, classify_regression, classify_regression_check, create_report,
    is_trusted_run, publish, runtime_summary, publish_event,
    should_replace, validate_record, validate_report,
)


def run(**changes):
    value = {
        'repository': 'NKU100/ImPad',
        'headRepository': 'NKU100/ImPad',
        'workflowPath': '.github/workflows/compatibility-wechat.yml',
        'event': 'workflow_dispatch', 'branch': 'main', 'status': 'completed',
        'conclusion': 'success', 'runId': 120, 'runNumber': 40,
        'runAttempt': 1, 'sha': 'a' * 40, 'appId': 'wechat',
    }
    value.update(changes)
    return value


def report(**changes):
    value = {
        'schemaVersion': 1, 'appId': 'wechat', 'runId': 120,
        'runNumber': 40, 'runAttempt': 1, 'sha': 'a' * 40,
        'candidateSha256': 'b' * 64, 'versionName': '8.0.60',
        'versionCode': 806000, 'outcome': 'VERIFIED', 'operation': 'verify',
        'candidateStatus': 'PREPARED', 'validationOutcome': 'VERIFIED',
        'adaptationAttempted': False, 'registrationStatus': '', 'prReady': False,
        'regressionOutcome': 'PASSED', 'checkOutcome': 'PASSED',
        'buildOutcome': 'PASSED', 'runtimeEnvironment': 'HEALTHY',
    }
    value.update(changes)
    return value


class CompatibilityBadgeTest(unittest.TestCase):
    def test_exact_verify_success_is_compatible_and_uses_candidate_version(self):
        result = classify(run(), report())
        self.assertEqual(('green', '已兼容', '8.0.60 · 已兼容'),
                         (result['state'], result['message'], result['version']))

    def test_complete_success_flags_without_exact_candidate_identity_stay_gray(self):
        missing_identity = report(candidateSha256=None, versionName=None, versionCode=None)
        self.assertEqual('gray', classify(run(), missing_identity)['state'])
        waiting_adaptation = report(
            operation='adapt', adaptationAttempted=True, outcome='PENDING_RUNTIME',
            validationOutcome='PENDING_RUNTIME', candidateSha256=None, versionName=None,
            versionCode=None,
        )
        self.assertEqual('gray', classify(run(), waiting_adaptation)['state'])

    def test_runtime_summary_command_is_not_exposed_as_a_cli_dead_path(self):
        import subprocess
        import sys

        result = subprocess.run([sys.executable, '-m', 'scripts.ci.compatibility_badge', '--help'],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn('runtime-summary', result.stdout)

    def test_adapt_operation_that_needs_no_change_is_green_after_runtime_passes(self):
        result = classify(run(), report(operation='adapt'))
        self.assertEqual('green', result['state'])

    def test_all_twelve_terminal_states_have_stable_colors_and_labels(self):
        cases = [
            ({}, ('green', '已兼容')),
            ({'operation': 'adapt', 'adaptationAttempted': True,
              'registrationStatus': 'REGISTERED', 'prReady': True}, ('yellow', '待合并')),
            ({'operation': 'adapt', 'adaptationAttempted': True,
              'outcome': 'PENDING_RUNTIME', 'validationOutcome': 'PENDING_RUNTIME'},
             ('yellow', '待验证')),
            ({'operation': 'verify', 'candidateStatus': 'INCOMPATIBLE',
              'outcome': 'NO_MATCH'}, ('orange', '需适配')),
            ({'operation': 'adapt', 'adaptationAttempted': True,
              'outcome': 'ADAPTATION_INCOMPLETE'}, ('orange', '适配未完成')),
            ({'regressionOutcome': 'FAILED', 'outcome': 'REGRESSION_FAILED'}, ('red', '回归失败')),
            ({'outcome': 'RUNTIME_INCOMPATIBLE', 'runtimeEnvironment': 'HEALTHY'},
             ('red', '验证未通过')),
            ({'checkOutcome': 'FAILED'}, ('red', '检查失败')),
            ({'buildOutcome': 'FAILED'}, ('red', '构建失败')),
            ({'outcome': 'ERROR'}, ('gray', '执行异常')),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                result = classify(run(), report(**changes))
                self.assertEqual(expected, (result['state'], result['message']))
        self.assertEqual('已取消', classify(run(conclusion='cancelled'), None)['message'])
        self.assertEqual('暂无记录', classify(None, None)['message'])

    def test_green_and_waiting_states_require_complete_stage_evidence(self):
        for changes in (
            {'checkOutcome': 'UNKNOWN'}, {'buildOutcome': 'UNKNOWN'},
            {'regressionOutcome': 'UNKNOWN'}, {'validationOutcome': 'UNKNOWN'},
        ):
            with self.subTest(changes=changes):
                self.assertNotEqual('green', classify(run(), report(**changes))['state'])
        incomplete_pr = report(operation='adapt', adaptationAttempted=True,
                               registrationStatus='REGISTERED', prReady=False)
        incomplete_registration = report(operation='adapt', adaptationAttempted=True,
                                         registrationStatus='', prReady=False)
        self.assertEqual('执行异常', classify(run(), incomplete_registration)['message'])
        self.assertNotEqual('待合并', classify(run(), incomplete_pr)['message'])

    def test_report_must_match_run_app_attempt_sha_and_candidate_identity(self):
        for changes in ({'runId': 121}, {'runAttempt': 2}, {'sha': 'c' * 40},
                        {'appId': 'qq'}, {'candidateSha256': ''},
                        {'versionName': '最新版'}):
            with self.subTest(changes=changes):
                self.assertEqual('gray', classify(run(), report(**changes))['state'])

    def test_only_official_main_app_entrypoints_and_terminal_start_events_are_trusted(self):
        self.assertTrue(is_trusted_run(run()))
        for changes in (
            {'repository': 'fork/ImPad'}, {'headRepository': 'fork/ImPad'},
            {'branch': 'feature'}, {'event': 'push'}, {'event': 'pull_request'},
            {'workflowPath': '.github/workflows/compatibility-app.yml'},
            {'workflowPath': '.github/workflows/build.yml'}, {'status': 'in_progress'},
            {'appId': 'other'},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(is_trusted_run(run(**changes)))

    def test_report_schema_is_exact_bounded_and_rejects_secrets_or_bad_versions(self):
        validate_report(report())
        for invalid in (
            report(extra='not allowed'), report(runAttempt='1'), report(sha='bad'),
            report(candidateSha256='x'), report(versionName='8.0.60?token=secret'),
            report(extra='x' * 5000),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_report(invalid)

    def test_report_builder_does_not_attach_a_new_candidate_to_a_restored_checkpoint(self):
        state = {
            'operation': 'adapt', 'outcome': 'ADAPTATION_INCOMPLETE',
            'candidateStatus': 'INCOMPATIBLE', 'adaptationAttempted': True,
            'registrationStatus': '', 'prReady': False, 'regressionOutcome': 'PASSED',
            'checkOutcome': 'PASSED', 'buildOutcome': 'PASSED',
            'runtimeEnvironment': 'UNKNOWN', 'pendingAdaptation': True,
            'candidateSource': 'rules-analysis', 'versionName': '8.0.61',
            'versionCode': 806100, 'candidateSha256': 'c' * 64,
        }
        result = create_report(run(), state)
        self.assertIsNone(result['versionName'])
        self.assertIsNone(result['versionCode'])
        self.assertIsNone(result['candidateSha256'])

    def test_report_builder_copies_only_allowlisted_workflow_evidence(self):
        state = {
            'operation': 'verify', 'outcome': 'VERIFIED', 'candidateStatus': 'PREPARED',
            'validationOutcome': 'VERIFIED', 'adaptationAttempted': False,
            'registrationStatus': '', 'prReady': False, 'regressionOutcome': 'PASSED',
            'checkOutcome': 'PASSED', 'buildOutcome': 'PASSED',
            'runtimeEnvironment': 'HEALTHY', 'candidateSource': 'verify',
            'versionName': '8.0.60', 'versionCode': 806000,
            'candidateSha256': 'b' * 64, 'token': 'must-not-escape',
        }
        result = create_report(run(), state)
        self.assertEqual(report(), result)
        self.assertNotIn('token', json.dumps(result))

    def test_runtime_summary_exports_exact_candidate_and_verifier_result_without_payloads(self):
        candidate = {
            'schemaVersion': 2, 'appId': 'wechat', 'status': 'PREPARED',
            'identity': {'versionName': '8.0.60', 'versionCode': 806000,
                         'apkSha256': 'b' * 64, 'sourceUrl': 'https://example.invalid/apk?token=secret'},
        }
        result = {
            'schemaVersion': 2, 'outcome': 'VERIFIED',
            'candidate': {'appId': 'wechat', 'identity': candidate['identity']},
            'runtime': {'status': 'RUNTIME_SMOKE_VERIFIED', 'raw': 'sensitive'},
        }
        summary = runtime_summary(candidate, result, {'status': 'PASSED'})
        self.assertEqual('b' * 64, summary['candidateSha256'])
        self.assertEqual('VERIFIED', summary['outcome'])
        self.assertEqual('HEALTHY', summary['runtimeEnvironment'])
        self.assertNotIn('sourceUrl', json.dumps(summary))
        self.assertNotIn('token', json.dumps(summary))
        self.assertNotIn('raw', json.dumps(summary))

    def test_runtime_summary_only_confirms_runtime_rejection_from_verifier_proof(self):
        candidate = {'schemaVersion': 2, 'appId': 'qq', 'status': 'PREPARED',
                     'identity': {'versionName': '9.1.0', 'versionCode': 9100,
                                  'apkSha256': 'c' * 64}}
        rejection = {'schemaVersion': 2, 'outcome': 'RUNTIME_INCOMPATIBLE',
                     'candidate': {'appId': 'qq', 'identity': candidate['identity']},
                     'runtime': {'status': 'RUNTIME_REJECTED'}}
        self.assertEqual('HEALTHY', runtime_summary(candidate, rejection, {'status': 'PASSED'})[
            'runtimeEnvironment'])
        rejection['runtime']['status'] = 'ACCEPTANCE_UNAVAILABLE'
        self.assertEqual('UNKNOWN', runtime_summary(candidate, rejection, {'status': 'PASSED'})[
            'runtimeEnvironment'])

    def test_runtime_summary_reads_fixed_regression_rows_without_treating_no_samples_as_pass(self):
        candidate = {'schemaVersion': 2, 'appId': 'qq', 'status': 'PREPARED',
                     'identity': {'versionName': '9.1.0', 'versionCode': 9100,
                                  'apkSha256': 'c' * 64}}
        runtime = {'schemaVersion': 2, 'candidate': {'appId': 'qq',
                   'identity': candidate['identity']}, 'outcome': 'REGRESSION_FAILED'}
        self.assertEqual('FAILED', runtime_summary(candidate, runtime,
                         [{'status': 'PASSED'}, {'status': 'FAILED',
                          'failureKind': 'check_failure', 'recordId': 'sample-1'}])['regressionOutcome'])
        self.assertEqual('PASSED', runtime_summary(candidate, None,
                         [{'status': 'PASSED'}, {'status': 'PASSED'}])['regressionOutcome'])
        self.assertEqual('UNKNOWN', runtime_summary(candidate, None,
                         [{'status': 'NO_HISTORICAL_SAMPLES'}])['regressionOutcome'])

    def test_only_confirmed_sample_mismatch_is_regression_failure(self):
        execution_error = [{'status': 'FAILED', 'failureKind': 'execution_failure',
                            'recordId': 'sample-1'}]
        sample_mismatch = [{'status': 'FAILED', 'failureKind': 'check_failure',
                            'recordId': 'sample-1'}]
        baseline_check = [{'status': 'FAILED', 'failureKind': 'check_failure'}]
        self.assertEqual('UNKNOWN', classify_regression(execution_error))
        self.assertEqual('FAILED', classify_regression(sample_mismatch))
        self.assertEqual('FAILED', classify_regression_check(baseline_check))
        self.assertEqual('UNKNOWN', classify_regression(baseline_check))

    def test_verifier_regression_label_without_sample_failure_evidence_stays_gray(self):
        result = report(outcome='REGRESSION_FAILED', validationOutcome='REGRESSION_FAILED',
                        regressionOutcome='UNKNOWN')
        self.assertEqual('gray', classify(run(), result)['state'])

    def test_runtime_summary_requires_sample_failure_kind_before_regression_red(self):
        candidate = {'schemaVersion': 2, 'appId': 'wechat', 'status': 'PREPARED',
                     'identity': {'versionName': '8.0.60', 'versionCode': 806000,
                                  'apkSha256': 'b' * 64}}
        verification = {
            'schemaVersion': 2, 'outcome': 'REGRESSION_FAILED',
            'candidate': {'appId': 'wechat', 'identity': candidate['identity']},
        }
        summary = runtime_summary(candidate, verification, [{
            'status': 'FAILED', 'failureKind': 'execution_failure', 'recordId': 'sample-1',
        }])
        self.assertEqual('UNKNOWN', summary['regressionOutcome'])
        result = report(outcome='REGRESSION_FAILED', validationOutcome='REGRESSION_FAILED',
                        regressionOutcome=summary['regressionOutcome'])
        self.assertEqual('gray', classify(run(), result)['state'])

    def test_runtime_summary_distinguishes_only_explicit_selection_reasons(self):
        candidate = {'schemaVersion': 2, 'appId': 'wecom', 'status': 'INCOMPATIBLE',
                     'diagnostic': {'selection': 'CONFLICT'}}
        summary = runtime_summary(candidate, None, {'status': 'PASSED'})
        self.assertEqual(('CONFLICT', 'INCOMPATIBLE'),
                         (summary['outcome'], summary['candidateStatus']))
        candidate['diagnostic']['selection'] = 'UNSUPPORTED'
        self.assertEqual('ERROR', runtime_summary(candidate, None, {'status': 'PASSED'})['outcome'])

    def test_rerun_order_uses_run_number_then_attempt(self):
        incoming = {'runNumber': 41, 'runAttempt': 1}
        self.assertTrue(should_replace({'runNumber': 40, 'runAttempt': 99}, incoming))
        self.assertTrue(should_replace({'runNumber': 41, 'runAttempt': 1},
                                       {'runNumber': 41, 'runAttempt': 2}))
        self.assertFalse(should_replace(incoming, {'runNumber': 41, 'runAttempt': 1}))
        self.assertFalse(should_replace(incoming, {'runNumber': 40, 'runAttempt': 99}))

    def test_published_record_schema_keeps_version_and_state_appearance_consistent(self):
        record = {
            'schemaVersion': 1, 'state': 'green', 'label': '微信适配',
            'message': '8.0.60 · 已兼容', 'color': 'brightgreen',
            'version': '8.0.60 · 已兼容', 'versionName': '8.0.60',
            'versionCode': 806000, 'candidateSha256': 'b' * 64,
            'appId': 'wechat', 'runNumber': 40,
            'runAttempt': 1, 'runId': 120, 'sha': 'a' * 40,
        }
        validate_record(record)
        with self.assertRaises(ValueError):
            validate_record(dict(record, color='red'))
        with self.assertRaises(ValueError):
            validate_record(dict(record, extra='no'))
        with self.assertRaises(ValueError):
            validate_record(dict(record, appId='qq'))
        with self.assertRaises(ValueError):
            validate_record(dict(record, label='QQ适配'))

    def test_each_application_uses_its_own_endpoint_label(self):
        labels = {'wechat': '微信适配', 'qq': 'QQ适配', 'wecom': '企微适配'}
        paths = {app: f'.github/workflows/compatibility-{app}.yml' for app in labels}
        for app_id, label in labels.items():
            with self.subTest(app=app_id):
                result = classify(run(appId=app_id, workflowPath=paths[app_id]),
                                  report(appId=app_id))
                self.assertEqual(label, result['label'])

    def test_publisher_does_not_accept_untrusted_completion_or_execute_artifact(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        event = {
            'repository': {'full_name': 'NKU100/ImPad'},
            'workflow_run': {
                'path': '.github/workflows/compatibility-wechat.yml',
                'event': 'pull_request', 'head_branch': 'main', 'status': 'completed',
                'conclusion': 'success', 'id': 120, 'run_number': 40, 'run_attempt': 1,
                'head_sha': 'a' * 40, 'head_repository': {'full_name': 'NKU100/ImPad'},
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / 'event.json'
            event_path.write_text(json.dumps(event))
            artifact = root / 'artifact'
            artifact.mkdir()
            (artifact / 'compatibility-badge.json').write_text('not executable content')
            with patch('scripts.ci.compatibility_badge.publish') as publish:
                self.assertFalse(publish_event(event_path, artifact))
                publish.assert_not_called()

    def test_publisher_emits_gray_for_missing_report_and_only_publishes_allowlisted_record(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        event = {
            'repository': {'full_name': 'NKU100/ImPad'},
            'workflow_run': {
                'path': '.github/workflows/compatibility-wechat.yml',
                'event': 'workflow_dispatch', 'head_branch': 'main', 'status': 'completed',
                'conclusion': 'success', 'id': 120, 'run_number': 40, 'run_attempt': 1,
                'head_sha': 'a' * 40, 'head_repository': {'full_name': 'NKU100/ImPad'},
            },
            'large_event_metadata': 'x' * 9000,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / 'event.json'
            event_path.write_text(json.dumps(event))
            artifact = root / 'artifact'
            artifact.mkdir()
            with patch('scripts.ci.compatibility_badge.publish') as publish:
                self.assertTrue(publish_event(event_path, artifact))
                record, app_id = publish.call_args.args
                self.assertEqual('wechat', app_id)
                self.assertEqual(('gray', '执行异常'), (record['state'], record['message']))
                self.assertEqual({'schemaVersion', 'state', 'label', 'message', 'color',
                                  'version', 'versionName', 'versionCode', 'candidateSha256',
                                  'appId', 'runNumber', 'runAttempt', 'runId', 'sha'},
                                 set(record))
                (artifact / 'compatibility-badge.json').write_text(json.dumps(report()))
                publish.reset_mock()
                self.assertTrue(publish_event(event_path, artifact))
                published = publish.call_args.args[0]
                self.assertEqual('8.0.60 · 已兼容', published['message'])
                self.assertEqual('微信适配', published['label'])
                self.assertEqual(('8.0.60', 806000, 'b' * 64),
                                 (published['versionName'], published['versionCode'],
                                  published['candidateSha256']))

    def test_local_compare_and_swap_preserves_other_application_file(self):
        import os
        import subprocess
        import tempfile
        from pathlib import Path

        def git(directory, *args):
            return subprocess.run(['git', *args], cwd=directory, check=True,
                                  capture_output=True, text=True).stdout.strip()

        def endpoint(app_id, run_number):
            return {
                'schemaVersion': 1, 'state': 'gray',
                'label': {'wechat': '微信适配', 'qq': 'QQ适配', 'wecom': '企微适配'}[app_id],
                'message': '暂无记录', 'color': 'lightgrey', 'version': '暂无记录',
                'versionName': None, 'versionCode': None, 'candidateSha256': None,
                'appId': app_id, 'runNumber': run_number, 'runAttempt': 1,
                'runId': run_number + 100, 'sha': 'a' * 40,
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / 'remote.git'
            seed = root / 'seed'
            clone = root / 'clone'
            git(root, 'init', '--bare', '--initial-branch=main', str(remote))
            git(root, 'init', '--initial-branch=main', str(seed))
            git(seed, 'config', 'user.name', 'Test')
            git(seed, 'config', 'user.email', 'test@example.invalid')
            (seed / 'README').write_text('test')
            git(seed, 'add', 'README')
            git(seed, 'commit', '-m', 'initial')
            git(seed, 'remote', 'add', 'origin', str(remote))
            git(seed, 'push', 'origin', 'main')
            git(seed, 'switch', '-c', 'compatibility-badge-data')
            qq = endpoint('qq', 52)
            (seed / 'qq.json').write_text(json.dumps(qq, separators=(',', ':')) + '\n')
            git(seed, 'add', 'qq.json')
            git(seed, 'commit', '-m', 'seed qq badge')
            git(seed, 'push', '-u', 'origin', 'compatibility-badge-data')
            git(root, 'clone', str(remote), str(clone))
            original_directory = Path.cwd()
            try:
                os.chdir(clone)
                wechat = endpoint('wechat', 53)
                self.assertTrue(publish(wechat, 'wechat'))
            finally:
                os.chdir(original_directory)
            actual_qq = json.loads(git(root, '--git-dir', str(remote), 'show',
                                       'refs/heads/compatibility-badge-data:qq.json'))
            actual_wechat = json.loads(git(root, '--git-dir', str(remote), 'show',
                                           'refs/heads/compatibility-badge-data:wechat.json'))
            self.assertEqual(qq, actual_qq)
            self.assertEqual(wechat, actual_wechat)


if __name__ == '__main__':
    unittest.main()
