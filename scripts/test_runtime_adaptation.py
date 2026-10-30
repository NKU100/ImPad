import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from contextlib import ExitStack

from scripts.ci import runtime_adaptation as runtime
from scripts.ci.codex_adaptation import sha256
from scripts.ci.app_policy import get_policy
from scripts.ci.semantic_migration import load_migration_bundle
from scripts.test_semantic_migration import ROOT as SEMANTIC_ROOT, SemanticMigrationTest


class RuntimeAdaptationTest(unittest.TestCase):
    def setUp(self):
        original_cwd = Path.cwd()
        self.addCleanup(os.chdir, original_cwd)
        for name in ['ROOT', 'EVIDENCE', 'stage', 'APP_ID', 'APP_POLICY', 'APP_PACKAGE', 'UI_STRATEGY']:
            original = getattr(runtime.smoke, name)
            self.addCleanup(setattr, runtime.smoke, name, original)

    @staticmethod
    def checker_candidate(directory, app_id, candidate_apk):
        directory.mkdir(parents=True, exist_ok=True)
        digest = sha256(candidate_apk)
        input_path = directory / 'apk-inputs' / f'0-{digest}.apk'
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_bytes(Path(candidate_apk).read_bytes())
        static = {
            'appId': app_id,
            'inputHashes': [{'path': input_path.relative_to(directory).as_posix(), 'sha256': digest}],
            'routeCandidates': ['Lfixture/Entry;->open()V'],
            'ruleCatalogSha256': 'a' * 64,
            'executionIdentity': {'ruleCatalogSha256': 'a' * 64},
        }
        static_path = directory / 'static-check.json'
        static_path.write_text(json.dumps(static))
        candidate = {
            'schemaVersion': 1,
            'appId': app_id,
            'status': 'PREPARED',
            'identity': {'apkSha256': digest},
            'expectedPlan': {'selection': 'UNIQUE', 'ruleId': f'{app_id}_fixture'},
            'apkFiles': [{'path': input_path.relative_to(directory).as_posix(), 'role': 'base',
                          'sha256': digest}],
            'staticReport': static_path.relative_to(directory).as_posix(),
            'staticReportSha256': sha256(static_path),
        }
        (directory / 'candidate-report.json').write_text(json.dumps(candidate))
        return candidate

    @staticmethod
    def passed_regression(app_id, reports, **kwargs):
        rows = [{'status': 'PASSED'}]
        reports.mkdir(parents=True, exist_ok=True)
        (reports / 'results.json').write_text(json.dumps(rows))
        return rows

    def test_accepts_a_schema2_candidate_bound_to_the_selected_complete_plan(self):
        bundle = load_migration_bundle(SEMANTIC_ROOT)
        report_factory = SemanticMigrationTest('runTest')
        for profile in bundle.profiles:
            with self.subTest(app_id=profile['appId']):
                static = report_factory.report(profile)
                candidate = {
                    'schemaVersion': 2, 'appId': profile['appId'], 'status': 'PREPARED',
                    'identity': profile['identity'], 'ruleCatalog': profile['ruleCatalog'],
                    'ruleCatalogSha256': profile['ruleCatalog']['ruleCatalogSha256'],
                    'snapshot': profile['snapshot'], 'expectedPlan': profile['expectedPlan'],
                    'expectedExecutionSha256': profile['expectedExecutionSha256'],
                }
                runtime.validate_prepared_candidate(candidate, static, profile['appId'], profile['identity'])
                self.assertNotIn('routeCandidates', static)
                self.assertEqual(profile['expectedPlan'], candidate['expectedPlan'])

                for mutation in (
                        lambda c, s: c.update(expectedPlan={}),
                        lambda c, s: c.update(ruleCatalogSha256='0' * 64),
                        lambda c, s: s['selection'].update(status='CONFLICT')):
                    changed_candidate = json.loads(json.dumps(candidate))
                    changed_static = json.loads(json.dumps(static))
                    mutation(changed_candidate, changed_static)
                    with self.assertRaises(ValueError):
                        runtime.validate_prepared_candidate(changed_candidate, changed_static,
                                                            profile['appId'], profile['identity'])

    def mock_static_runtime_helpers(self, apk):
        return (
            patch.object(runtime, 'prepare_module', return_value={'sha256': sha256(apk)}),
            patch.object(runtime, 'verify_module_identity'),
            patch.object(runtime, 'run_regression', side_effect=self.passed_regression),
            patch.object(runtime, 'evaluate_validation', return_value={'outcome': 'VERIFIED'}),
        )

    def test_runtime_passes_full_reports_to_the_real_validation_gate(self):
        bundle = load_migration_bundle(SEMANTIC_ROOT)
        profile = next(row for row in bundle.profiles if row['appId'] == 'wechat')
        static = SemanticMigrationTest('runTest').report(profile)
        candidate = {'schemaVersion': 2, 'appId': profile['appId'], 'status': 'PREPARED',
                     'identity': profile['identity'], 'ruleCatalog': profile['ruleCatalog'],
                     'ruleCatalogSha256': profile['ruleCatalog']['ruleCatalogSha256'],
                     'snapshot': profile['snapshot'], 'expectedPlan': profile['expectedPlan'],
                     'expectedExecutionSha256': profile['expectedExecutionSha256']}
        runtime.validate_prepared_candidate(candidate, static, profile['appId'], profile['identity'])
        for invalid in ('CONFLICT', 'INCOMPLETE'):
            report = json.loads(json.dumps(static))
            report['selection']['status'] = invalid
            with self.subTest(selection=invalid), self.assertRaises(ValueError):
                runtime.validate_prepared_candidate(candidate, report, profile['appId'], profile['identity'])

    def test_initializer_refuses_a_physical_or_additional_device_before_setup(self):
        for devices in ['List of devices attached\n127.0.0.1:16556\tdevice\n',
                        'List of devices attached\nphone\tdevice\n',
                        'List of devices attached\nemulator-5554\tdevice\nphone\tdevice\n']:
            with patch.object(runtime.subprocess, 'check_output', return_value=devices), patch.object(runtime, 'AppSmoke') as setup:
                with self.assertRaisesRegex(RuntimeError, 'exactly one disposable'):
                    runtime.initialize(Path('/unused'), Path('/unused'), 'emulator-5554')
                setup.assert_not_called()

    def test_initializer_uses_the_explicit_policy_apk_and_package(self):
        policy = get_policy('wechat')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / 'task'; directory.mkdir()
            worktree = root / ('deep-' * 20) / '非ASCII' / 'checkout'; worktree.mkdir(parents=True)
            runtime_root = root / 'runtime'; runtime_root.mkdir()
            (runtime_root / 'wechat.apk').write_bytes(b'app specific apk')
            report = {'appId': 'wechat', 'identity': {'apkSha256': sha256(runtime_root / 'wechat.apk')}}
            state = {'appId': 'wechat', 'report': report, 'worktree': str(worktree)}
            state_path = directory / 'state.json'; state_path.write_text(json.dumps(state))
            listing = 'List of devices attached\n127.0.0.1:16555\tdevice\n'
            with patch.dict('os.environ', {'ADB_SERVER_SOCKET': 'tcp:localhost:5039'}, clear=False), \
                 patch.object(runtime.subprocess, 'check_output', side_effect=[listing, listing]), \
                 patch.object(runtime.subprocess, 'run') as run_command, \
                 patch.object(runtime, 'query_target'), patch.object(runtime, 'AppSmoke') as app, \
                 patch.object(runtime.smoke, 'configure_manager'), patch.object(runtime.smoke, 'adb'), \
                 patch.object(runtime.smoke, 'mobile_input', return_value='<hierarchy/>'):
                runtime.initialize(directory, runtime_root, '127.0.0.1:16555')
                self.assertEqual('tcp:localhost:5039', os.environ['ADB_SERVER_SOCKET'])
            app.return_value.setup.assert_called_once()
            self.assertEqual('wechat', runtime.smoke.APP_ID)
            self.assertEqual(3, run_command.call_count)
            self.assertFalse(any(call.args[0][-1] == 'kill-server' for call in run_command.call_args_list))
            self.assertEqual('start-server', run_command.call_args_list[0].args[0][-1])
            state = json.loads(state_path.read_text())
            self.assertTrue(state['runtime']['serverOwned'])
            self.assertTrue(state['runtime']['socket'].startswith('localfilesystem:'))
            endpoint_path = state['runtime']['socket'].removeprefix('localfilesystem:')
            self.assertFalse(Path(endpoint_path).is_absolute())
            self.assertLess(len(os.fsencode(endpoint_path)), 108)
            self.assertEqual(worktree.resolve(), Path.cwd())
            self.assertEqual(worktree.resolve(), Path(run_command.call_args_list[0].kwargs['cwd']).resolve())
            with patch.dict('os.environ', {}, clear=True):
                configured, _ = runtime.configure(directory)
                self.assertEqual(configured['runtime']['socket'], os.environ['ADB_SERVER_SOCKET'])
            device_instructions = json.loads((worktree / 'work/analysis/runtime-device.json').read_text())
            self.assertEqual('wechat', device_instructions['appId'])
            self.assertIn(policy.package_name, device_instructions['instructions'])

    def test_only_owned_runtime_server_is_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_path = directory / 'state.json'
            state_path.write_text(json.dumps({'runtime': {
                'socket': 'tcp:localhost:5039', 'serverOwned': False}}))
            with patch.object(runtime.subprocess, 'run') as run_command:
                self.assertFalse(runtime.stop_owned_adb_server(directory))
                run_command.assert_not_called()

            state_path.write_text(json.dumps({'worktree': str(directory), 'runtime': {
                'socket': 'localfilesystem:/tmp/impad-private.sock', 'serverOwned': True}}))
            with patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run_command:
                self.assertTrue(runtime.stop_owned_adb_server(directory))
            call = run_command.call_args
            self.assertEqual(['adb', '-L', 'localfilesystem:/tmp/impad-private.sock', 'kill-server'],
                             call.args[0])
            self.assertEqual('localfilesystem:/tmp/impad-private.sock', call.kwargs['env']['ADB_SERVER_SOCKET'])
            self.assertEqual(directory.resolve(), call.kwargs['cwd'])

    def test_configure_uses_workspace_as_cwd_for_relative_private_endpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / 'task'; task.mkdir()
            workspace = root / '非ASCII' / ('deep-' * 20)
            workspace.mkdir(parents=True)
            (task / 'state.json').write_text(json.dumps({
                'appId': 'wechat', 'worktree': str(workspace),
                'runtime': {'root': str(root), 'serial': 'emulator-5554',
                            'socket': 'localfilesystem:.impad-adb-private.sock'}}))
            runtime.configure(task)
            self.assertEqual(workspace.resolve(), Path.cwd())
            self.assertEqual('localfilesystem:.impad-adb-private.sock', os.environ['ADB_SERVER_SOCKET'])

    def test_rejects_schema1_candidate_before_runtime_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, workspace, device = root / 'task', root / 'checkout', root / 'device'
            task.mkdir(); device.mkdir(); workspace.mkdir()
            apk = workspace / 'app/build/outputs/apk/debug/app-debug.apk'
            apk.parent.mkdir(parents=True); apk.write_bytes(b'module')
            candidate_apk = device / 'wechat.apk'; candidate_apk.write_bytes(b'candidate')
            candidate = self.checker_candidate(workspace / 'work/runtime-smoke', 'wechat', candidate_apk)
            (task / 'state.json').write_text(json.dumps({'appId': 'wechat', 'worktree': str(workspace), 'report': candidate,
                'runtime': {'root': str(device), 'socket': str(root / 'socket'), 'serial': 'emulator-5554'}}))
            with patch.dict('os.environ'), patch.object(runtime.smoke, 'adb') as adb:
                with patch.object(runtime.smoke, 'snapshot'), patch.object(runtime.smoke, 'collect_logs'):
                    runtime.smoke.EVIDENCE = task / 'failure-evidence'
                    runtime.smoke.EVIDENCE.mkdir()
                    with self.assertRaisesRegex(ValueError, 'exact shared-checker candidate'):
                        runtime.verify_runtime(task)
            adb.assert_not_called()

    def test_all_apps_use_complete_schema2_plan_reports_without_numbered_routes(self):
        bundle = load_migration_bundle(SEMANTIC_ROOT)
        factory = SemanticMigrationTest('runTest')
        for profile in bundle.profiles:
            with self.subTest(app_id=profile['appId']):
                static = factory.report(profile)
                candidate = {'schemaVersion': 2, 'appId': profile['appId'], 'status': 'PREPARED',
                             'identity': profile['identity'], 'ruleCatalog': profile['ruleCatalog'],
                             'ruleCatalogSha256': profile['ruleCatalog']['ruleCatalogSha256'],
                             'snapshot': profile['snapshot'], 'expectedPlan': profile['expectedPlan'],
                             'expectedExecutionSha256': profile['expectedExecutionSha256']}
                runtime.validate_prepared_candidate(candidate, static, profile['appId'], profile['identity'])
                self.assertTrue(candidate['expectedPlan']['hooks'])
                self.assertNotIn('routeCandidates', static)

    def test_rules_runtime_rejects_legacy_candidate_before_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, workspace, device = root / 'task', root / 'checkout', root / 'device'
            directory.mkdir(); workspace.mkdir(); device.mkdir()
            apk = workspace / 'app/build/outputs/apk/debug/app-debug.apk'
            apk.parent.mkdir(parents=True); apk.write_bytes(b'module')
            candidate_apk = device / 'wechat.apk'; candidate_apk.write_bytes(b'candidate')
            candidate = self.checker_candidate(workspace / 'work/runtime-smoke', 'wechat', candidate_apk)
            (directory / 'state.json').write_text(json.dumps({'appId': 'wechat', 'worktree': str(workspace), 'report': candidate,
                'runtime': {'root': str(device), 'socket': str(root / 'socket'), 'serial': 'emulator-5554'}}))
            with patch.dict('os.environ'), patch.object(runtime.smoke, 'adb') as adb, \
                 patch.object(runtime.smoke, 'snapshot'), patch.object(runtime.smoke, 'collect_logs'):
                runtime.smoke.EVIDENCE = directory / 'failure-evidence'
                runtime.smoke.EVIDENCE.mkdir()
                with self.assertRaisesRegex(ValueError, 'exact shared-checker candidate'):
                    runtime.verify_runtime(directory)
            adb.assert_not_called()

    def test_runtime_candidate_identity_must_match_refreshed_checker_report(self):
        bundle = load_migration_bundle(SEMANTIC_ROOT)
        profile = next(row for row in bundle.profiles if row['appId'] == 'wechat')
        static = SemanticMigrationTest('runTest').report(profile)
        candidate = {'schemaVersion': 2, 'appId': profile['appId'], 'status': 'PREPARED',
                     'identity': profile['identity'], 'ruleCatalog': profile['ruleCatalog'],
                     'ruleCatalogSha256': profile['ruleCatalog']['ruleCatalogSha256'],
                     'snapshot': profile['snapshot'], 'expectedPlan': profile['expectedPlan'],
                     'expectedExecutionSha256': profile['expectedExecutionSha256']}
        runtime.validate_prepared_candidate(candidate, static, profile['appId'], profile['identity'])
        stale = dict(profile['identity'], versionCode=profile['identity']['versionCode'] + 1)
        with self.assertRaisesRegex(ValueError, 'exact shared-checker candidate'):
            runtime.validate_prepared_candidate(candidate, static, profile['appId'], stale)

    def test_semantic_candidate_reports_use_confined_relative_artifact_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_root = root / 'candidate'; artifact_root.mkdir()
            report = artifact_root / 'static-check.json'; report.write_text('{}')
            self.assertEqual(report.resolve(), runtime.resolve_candidate_artifact_path(
                artifact_root, 'static-check.json', 'Static report'))
            for reference in ('../outside.json', str(report.resolve()), ''):
                with self.subTest(reference=reference), self.assertRaises(ValueError):
                    runtime.resolve_candidate_artifact_path(artifact_root, reference, 'Static report')

    def test_candidate_artifact_path_guard_accepts_relative_path_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_root = root / 'candidate'; artifact_root.mkdir()
            apk = artifact_root / 'apk/base.apk'; apk.parent.mkdir(); apk.write_bytes(b'apk')
            self.assertEqual(apk.resolve(), runtime.resolve_candidate_artifact_path(
                artifact_root, 'apk/base.apk', 'Candidate APK'))
            for reference in ('../outside.apk', str(apk.resolve()), ''):
                with self.subTest(reference=reference), self.assertRaises(ValueError):
                    runtime.resolve_candidate_artifact_path(artifact_root, reference, 'Candidate APK')

    def test_runtime_rejects_a_prepared_report_for_a_different_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, workspace, artifact = root / 'task', root / 'checkout', root / 'artifact'
            directory.mkdir(); workspace.mkdir(); artifact.mkdir()
            module = workspace / 'app/build/outputs/apk/debug/app-debug.apk'
            module.parent.mkdir(parents=True); module.write_bytes(b'module')
            candidate_apk = artifact / 'wechat.apk'; candidate_apk.write_bytes(b'exact candidate')
            prepared = self.checker_candidate(workspace / 'work/runtime-smoke', 'wechat', candidate_apk)
            original = dict(prepared, status='INCOMPATIBLE')
            prepared['identity'] = dict(prepared['identity'], apkSha256='b' * 64)
            (workspace / 'work/runtime-smoke/candidate-report.json').write_text(json.dumps(prepared))
            state = {'appId': 'wechat', 'worktree': str(workspace), 'report': original,
                     'runtime': {'root': str(artifact), 'socket': str(root / 'socket'),
                                 'serial': 'emulator-5554'}}
            (directory / 'state.json').write_text(json.dumps(state))
            with patch.dict('os.environ'), patch.object(runtime.smoke, 'snapshot'), \
                 patch.object(runtime.smoke, 'collect_logs'), patch.object(runtime.smoke, 'adb') as adb:
                with self.assertRaisesRegex(ValueError, 'differs from the exact shared-checker candidate'):
                    runtime.verify_runtime(directory)
            adb.assert_not_called()

    def test_probe_failure_restores_iteration_evidence_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / 'iteration-001'; evidence.mkdir()
            with patch.object(runtime, 'AppSmoke') as app:
                def fail(candidate):
                    runtime.smoke.EVIDENCE = root / 'apps/qq'
                    raise RuntimeError('Restart failed')
                app.return_value.run_candidate.side_effect = fail
                with self.assertRaisesRegex(RuntimeError, 'Restart failed'):
                    runtime.probe_candidate(root, evidence, {'appId': 'qq'})
                self.assertEqual(runtime.smoke.EVIDENCE, evidence)

    def test_diagnostics_include_fresh_hook_records_and_exclude_secondary_processes(self):
        runtime.smoke.configure_app_id('wechat')
        diagnostics = runtime.smoke.hook_diagnostics('\n'.join([
            'LSPosedFramework (com.tencent.mm)[ImPad] status=COMPATIBLE',
            'LSPosedFramework (com.tencent.mm)[ImPad] resolved tablet=Lfixture/A;->a()Z login=Lfixture/B;->b()V',
            'LSPosedFramework (com.tencent.mm)[ImPad] installed 2 WeChat hooks',
            'LSPosedFramework [ImPad] process skipped: com.tencent.mm:push']))
        self.assertEqual('OBSERVED', diagnostics['moduleLoaded'])
        self.assertEqual('INSTALLED_2', diagnostics['hookInstallation'])
        self.assertEqual(1, len(diagnostics['resolvedHooks']))
        self.assertEqual(3, len(diagnostics['records']))

    def test_absent_logs_are_not_proof_of_injection_failure(self):
        diagnostics = runtime.smoke.hook_diagnostics('ActivityManager installed ImPad\n')
        self.assertEqual('NOT_OBSERVED', diagnostics['moduleLoaded'])
        self.assertEqual('NOT_OBSERVED', diagnostics['hookInstallation'])


if __name__ == '__main__':
    unittest.main()
