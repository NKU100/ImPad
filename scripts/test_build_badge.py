import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.ci.build_badge import (classify, is_trusted_run, validate_report,
                                    should_replace, unit_tests_outcome,
                                    gradle_failure_outcome)
from scripts.ci.build_badge import gradle_test_failures
from scripts.ci.build_badge import publish_event
from scripts.ci.build_badge import publish


def run(**overrides):
    value = {
        'repository': 'NKU100/ImPad', 'workflowPath': '.github/workflows/build.yml',
        'headRepository': 'NKU100/ImPad',
        'event': 'push', 'branch': 'main', 'status': 'completed', 'conclusion': 'success',
        'runId': 120, 'runNumber': 40, 'runAttempt': 1,
        'sha': 'a' * 40,
    }
    value.update(overrides)
    return value


def report(**overrides):
    value = {
        'schemaVersion': 1, 'runId': 120, 'runAttempt': 1, 'sha': 'a' * 40,
        'unitTests': 'passed', 'staticRegression': 'passed',
        'moduleBuild': 'passed', 'artifactUpload': 'passed',
    }
    value.update(overrides)
    return value


class BuildBadgeTest(unittest.TestCase):
    def test_green_requires_every_required_check_build_signing_and_upload(self):
        result = classify(run(), report())
        self.assertEqual('green', result['state'])
        self.assertEqual('通过', result['message'])
        for field, value in [('unitTests', 'unknown'), ('staticRegression', 'execution_failure'),
                             ('moduleBuild', 'unknown'), ('artifactUpload', 'failed')]:
            with self.subTest(field=field):
                self.assertNotEqual('green', classify(run(), report(**{field: value}))['state'])

    def test_structured_matcher_failure_remains_red_when_another_sample_download_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'results.json'
            path.write_text(json.dumps([
                {'status': 'FAILED', 'failureKind': 'check_failure'},
                {'status': 'FAILED', 'failureKind': 'execution_failure'},
            ]))
            from scripts.ci.build_badge import _static_outcome
            self.assertEqual('check_failure', _static_outcome(path))

    def test_explicit_check_and_build_failures_are_red_but_execution_anomaly_is_gray(self):
        self.assertEqual('red', classify(run(conclusion='failure'), report(unitTests='failed'))['state'])
        self.assertEqual('red', classify(run(conclusion='failure'), report(staticRegression='check_failure'))['state'])
        self.assertEqual('red', classify(run(conclusion='failure'), report(moduleBuild='failed'))['state'])
        self.assertEqual('gray', classify(run(conclusion='failure'), report(staticRegression='execution_failure'))['state'])
        self.assertEqual('gray', classify(run(conclusion='timed_out'), None)['state'])
        missing = classify(run(), None)
        self.assertEqual('gray', missing['state'])
        self.assertEqual('执行异常', missing['message'])
        mismatched = classify(run(), report(runId=121))
        self.assertEqual('执行异常', mismatched['message'])

    def test_cancelled_run_is_gray_and_success_with_missing_evidence_is_not_green(self):
        result = classify(run(conclusion='cancelled'), report())
        self.assertEqual('gray', result['state'])
        self.assertEqual('已取消', result['message'])
        self.assertEqual('gray', classify(run(), None)['state'])
        bootstrap = classify(None, None)
        self.assertEqual('gray', bootstrap['state'])
        self.assertEqual('暂无记录', bootstrap['message'])

    def test_evidence_must_match_run_id_attempt_and_commit(self):
        for changes in ({'runId': 121}, {'runAttempt': 2}, {'sha': 'b' * 40}):
            with self.subTest(changes=changes):
                self.assertEqual('gray', classify(run(), report(**changes))['state'])

    def test_trust_filter_accepts_only_official_main_push_from_build_workflow(self):
        self.assertTrue(is_trusted_run(run()))
        for changes in ({'repository': 'fork/ImPad'}, {'branch': 'feature'}, {'event': 'pull_request'},
                        {'workflowPath': '.github/workflows/compatibility-wechat.yml'},
                        {'status': 'in_progress'}):
            with self.subTest(changes=changes):
                self.assertFalse(is_trusted_run(run(**changes)))
        self.assertFalse(is_trusted_run(run(runAttempt=0)))

    def test_schema_rejects_extra_large_or_malformed_evidence(self):
        validate_report(report())
        for invalid in (report(extra='no'), report(runId='120'), report(sha='bad'),
                        report(unitTests='maybe')):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_report(invalid)
        with self.assertRaises(ValueError):
            validate_report(json.loads('{"schemaVersion":1}') | {'padding': 'x' * 20000})

    def test_publisher_binds_artifact_to_workflow_run_metadata_and_bootstraps_gray_without_report(self):
        event = {
            'repository': {'full_name': 'NKU100/ImPad'},
            'workflow_run': {
                'path': '.github/workflows/build.yml', 'event': 'push', 'head_branch': 'main',
                'head_repository': {'full_name': 'NKU100/ImPad'}, 'id': 120,
                'run_number': 40, 'run_attempt': 2, 'head_sha': 'a' * 40,
                'status': 'completed', 'conclusion': 'success',
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / 'event.json'
            event_path.write_text(json.dumps(event))
            artifacts = root / 'artifact'
            artifacts.mkdir()
            (artifacts / 'build-badge.json').write_text(json.dumps(
                report(runAttempt=2, runId=120)))
            with patch('scripts.ci.build_badge.publish', return_value=True) as publisher:
                self.assertTrue(publish_event(event_path, artifacts))
            record = publisher.call_args.args[0]
            self.assertEqual((120, 2, 'a' * 40),
                             (record['runId'], record['runAttempt'], record['sha']))
            self.assertEqual('green', record['state'])
            (artifacts / 'build-badge.json').unlink()
            with patch('scripts.ci.build_badge.publish', return_value=True) as publisher:
                publish_event(event_path, artifacts)
            self.assertEqual('执行异常', publisher.call_args.args[0]['message'])

    def test_publisher_ignores_workflow_run_that_is_not_completed(self):
        event = {
            'repository': {'full_name': 'NKU100/ImPad'},
            'workflow_run': {
                'path': '.github/workflows/build.yml', 'event': 'push', 'head_branch': 'main',
                'head_repository': {'full_name': 'NKU100/ImPad'}, 'id': 120,
                'run_number': 40, 'run_attempt': 1, 'head_sha': 'a' * 40,
                'status': 'in_progress', 'conclusion': None,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / 'event.json'
            event_path.write_text(json.dumps(event))
            with patch('scripts.ci.build_badge.publish') as publisher:
                self.assertFalse(publish_event(event_path, root))
            publisher.assert_not_called()

    def test_older_run_or_attempt_cannot_overwrite_published_state(self):
        current = {'runNumber': 41, 'runAttempt': 1}
        self.assertFalse(should_replace(current, {'runNumber': 40, 'runAttempt': 9}))
        self.assertFalse(should_replace(current, {'runNumber': 41, 'runAttempt': 1}))
        self.assertTrue(should_replace(current, {'runNumber': 41, 'runAttempt': 2}))
        self.assertTrue(should_replace(current, {'runNumber': 42, 'runAttempt': 1}))
        self.assertTrue(should_replace(None, {'runNumber': 1, 'runAttempt': 1}))

    def test_badge_branch_bootstraps_and_compare_and_swap_rejects_stale_publications(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / 'remote.git'
            checkout = root / 'checkout'
            subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(remote)], check=True,
                           capture_output=True)
            subprocess.run(['git', 'init', '--initial-branch=main', str(checkout)], check=True,
                           capture_output=True)
            subprocess.run(['git', '-C', str(checkout), 'remote', 'add', 'origin', str(remote)], check=True)
            (checkout / 'seed').write_text('trusted publisher source')
            subprocess.run(['git', '-C', str(checkout), 'add', 'seed'], check=True)
            subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=Fixture', '-c',
                            'user.email=fixture@example.invalid', 'commit', '-m', 'seed'], check=True,
                           capture_output=True)
            subprocess.run(['git', '-C', str(checkout), 'push', '-u', 'origin', 'main'], check=True,
                           capture_output=True)
            original = Path.cwd()
            try:
                os.chdir(checkout)
                first = {'schemaVersion': 1, 'state': 'green', 'label': '构建', 'message': '通过',
                         'color': 'brightgreen', 'runNumber': 40, 'runAttempt': 1,
                         'runId': 120, 'sha': 'a' * 40}
                self.assertTrue(publish(first))
                second = dict(first, runNumber=41, runAttempt=1, runId=121, state='red',
                              message='检查失败', color='red')
                self.assertTrue(publish(second))
                self.assertFalse(publish(first))
                rerun = dict(second, runAttempt=2, runId=121, state='gray',
                             message='已取消', color='lightgrey')
                self.assertTrue(publish(rerun))
                current = subprocess.run(['git', '--git-dir', str(remote), 'show',
                                          'refs/heads/build-badge-data:build.json'], check=True,
                                         text=True, capture_output=True).stdout
                self.assertEqual('gray', json.loads(current)['state'])
                self.assertEqual(2, json.loads(current)['runAttempt'])
            finally:
                os.chdir(original)

    def test_unit_test_evidence_marks_assertion_failures_red_and_runner_errors_gray(self):
        self.assertEqual('passed', unit_tests_outcome({'testsRun': 8, 'failures': 0, 'errors': 0}))
        self.assertEqual('failed', unit_tests_outcome({'testsRun': 8, 'failures': 1, 'errors': 0}))
        self.assertEqual('unknown', unit_tests_outcome({'testsRun': 8, 'failures': 0, 'errors': 1}))
        self.assertEqual('failed', unit_tests_outcome({'testsRun': 8, 'failures': 0, 'errors': 0,
                                                       'unexpectedSuccesses': 1, 'wasSuccessful': False}))
        self.assertEqual('unknown', unit_tests_outcome({'testsRun': 8, 'failures': 0, 'errors': 0,
                                                        'unexpectedSuccesses': 0, 'wasSuccessful': True,
                                                        'shouldStop': True}))

    def test_gradle_junit_failure_is_read_as_structured_assertion_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / 'app/build/test-results/testReleaseUnitTest/TEST-sample.xml'
            report.parent.mkdir(parents=True)
            report.write_text('<testsuite><testcase><failure message="assertion"/></testcase></testsuite>')
            started = report.stat().st_mtime_ns - 1
            self.assertEqual(1, gradle_test_failures(root, started))
            self.assertIsNone(gradle_test_failures(root, report.stat().st_mtime_ns + 1))

    def test_gradle_failure_is_red_only_for_explicit_compile_r8_package_or_sign_tasks(self):
        evidence = lambda task, causes=None: {
            'schemaVersion': 1, 'failedTasks': [task], 'causes': causes or ['CompilationFailedException']}
        self.assertEqual('unknown', gradle_failure_outcome(evidence(
            ':app:compileReleaseKotlin', ['org.gradle.api.tasks.TaskExecutionException'])))
        self.assertEqual('build_failure', gradle_failure_outcome(evidence(':app:compileReleaseKotlin')))
        self.assertEqual('build_failure', gradle_failure_outcome(evidence(':app:minifyReleaseWithR8')))
        self.assertEqual('build_failure', gradle_failure_outcome(evidence(':app:packageRelease')))
        self.assertEqual('build_failure', gradle_failure_outcome(evidence(':app:signRelease')))
        self.assertEqual('check_failure', gradle_failure_outcome(evidence(':app:verifyModuleMetadata')))
        self.assertEqual('check_failure', gradle_failure_outcome(evidence(':app:verifyReleaseModuleMetadata')))
        self.assertEqual('unknown', gradle_failure_outcome(evidence(':app:resolveReleaseDependencies')))
        self.assertEqual('unknown', gradle_failure_outcome(evidence(':app:compileReleaseKotlin',
                                                                     ['ModuleVersionResolveException'])))
        for cause in ('OutOfMemoryError', 'java.io.IOException', 'FileSystemException',
                      'GradleDaemonDisappearedException'):
            with self.subTest(cause=cause):
                self.assertEqual('unknown', gradle_failure_outcome(
                    evidence(':app:compileReleaseKotlin', [cause])))
        self.assertEqual('unknown', gradle_failure_outcome({'schemaVersion': 1,
                                                            'failedTasks': [], 'causes': []}))

    def test_gradle_module_metadata_variants_map_to_check_not_build_failure(self):
        from scripts.ci.build_badge import write_evidence
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            try:
                os.chdir(root)
                evidence_dir = Path('work/build-badge')
                evidence_dir.mkdir(parents=True)
                (evidence_dir / 'unit-tests.json').write_text(json.dumps({
                    'testsRun': 3, 'failures': 0, 'errors': 0,
                    'unexpectedSuccesses': 0, 'shouldStop': False, 'wasSuccessful': True,
                }))
                (evidence_dir / 'module-build.json').write_text('{"schemaVersion":1,"outcome":"unknown"}')
                for task in (':app:verifyModuleMetadata', ':app:verifyReleaseModuleMetadata'):
                    (evidence_dir / 'gradle-failures.json').write_text(json.dumps({
                        'schemaVersion': 1, 'failedTasks': [task], 'causes': ['GradleException'],
                    }))
                    with patch.dict(os.environ, {
                        'GRADLE_TESTS_OUTCOME': 'failure', 'MODULE_UPLOAD_OUTCOME': 'skipped',
                        'GITHUB_RUN_ID': '120', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40,
                    }):
                        output = root / (task.rsplit(':', 1)[-1] + '.json')
                        write_evidence(output)
                    report = json.loads(output.read_text())
                    self.assertEqual('failed', report['unitTests'], task)
                    self.assertEqual('unknown', report['moduleBuild'], task)
            finally:
                os.chdir(original)

    def test_json_reader_requests_at_most_limit_plus_one_bytes(self):
        from scripts.ci.build_badge import _read_json
        handle = MagicMock()
        handle.__enter__.return_value.read.return_value = b'xxx'
        with patch.object(Path, 'open', return_value=handle) as open_file:
            self.assertIsNone(_read_json('/tmp/badge.json', limit=2))
        open_file.assert_called_once_with('rb')
        handle.__enter__.return_value.read.assert_called_once_with(3)

    def test_unit_test_runner_preserves_unexpected_success_and_stop_evidence(self):
        from contextlib import redirect_stderr
        from scripts.ci.build_badge import unit_tests_outcome
        from scripts.ci.run_unit_tests import main

        class UnexpectedSuccess(unittest.TestCase):
            @unittest.expectedFailure
            def test_unexpected_success(self):
                pass

        class StoppedSuite(unittest.TestCase):
            def test_marks_result_stopped(self):
                self._outcome.result.shouldStop = True

        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            try:
                os.chdir(temporary)
                with patch('scripts.ci.run_unit_tests.unittest.defaultTestLoader.discover',
                           return_value=unittest.defaultTestLoader.loadTestsFromTestCase(UnexpectedSuccess)), \
                     redirect_stderr(io.StringIO()):
                    self.assertEqual(1, main())
                evidence_path = Path('work/build-badge/unit-tests.json')
                evidence = json.loads(evidence_path.read_text())
                self.assertEqual(1, evidence['unexpectedSuccesses'])
                self.assertFalse(evidence['wasSuccessful'])
                self.assertEqual('failed', unit_tests_outcome(evidence))

                with patch('scripts.ci.run_unit_tests.unittest.defaultTestLoader.discover',
                           return_value=unittest.defaultTestLoader.loadTestsFromTestCase(StoppedSuite)), \
                     redirect_stderr(io.StringIO()):
                    self.assertEqual(0, main())
                evidence = json.loads(evidence_path.read_text())
                self.assertTrue(evidence['shouldStop'])
                self.assertTrue(evidence['wasSuccessful'])
                self.assertEqual('unknown', unit_tests_outcome(evidence))
            finally:
                os.chdir(original)


if __name__ == '__main__':
    unittest.main()
