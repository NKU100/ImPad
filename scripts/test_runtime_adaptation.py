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


class RuntimeAdaptationTest(unittest.TestCase):
    def setUp(self):
        original_cwd = Path.cwd()
        self.addCleanup(os.chdir, original_cwd)
        for name in ['ROOT', 'EVIDENCE', 'stage', 'APP_ID', 'APP_POLICY', 'APP_PACKAGE', 'UI_STRATEGY']:
            original = getattr(runtime.smoke, name)
            self.addCleanup(setattr, runtime.smoke, name, original)

    @staticmethod
    def checker_candidate(directory, app_id, candidate_apk):
        digest = sha256(candidate_apk)
        static = {
            'appId': app_id,
            'inputHashes': [{'path': str(candidate_apk), 'sha256': digest}],
            'routeCandidates': ['Lfixture/Entry;->open()V'],
            'ruleCatalogSha256': 'a' * 64,
            'executionIdentity': {'ruleCatalogSha256': 'a' * 64},
        }
        static_path = directory / 'static-check.json'
        static_path.write_text(json.dumps(static))
        return {
            'appId': app_id,
            'identity': {'apkSha256': digest},
            'expectedPlan': {'selection': 'UNIQUE', 'ruleId': f'{app_id}_fixture'},
            'apkFiles': [{'path': str(candidate_apk), 'sha256': digest}],
            'staticReport': str(static_path),
            'staticReportSha256': sha256(static_path),
        }

    def mock_static_runtime_helpers(self, apk):
        return (
            patch.object(runtime, 'prepare_module', return_value={'sha256': sha256(apk)}),
            patch.object(runtime, 'verify_module_identity'),
            patch.object(runtime, 'run_regression', return_value=[]),
            patch.object(runtime, 'evaluate_validation', return_value={'outcome': 'VERIFIED'}),
        )

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

    def test_runtime_failure_copies_evidence_for_same_goal_feedback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / 'task'
            workspace = root / 'checkout'
            device = root / 'device'
            directory.mkdir(); device.mkdir()
            apk = workspace / 'app/build/outputs/apk/debug/app-debug.apk'
            apk.parent.mkdir(parents=True); apk.write_bytes(b'module')
            wechat = device / 'wechat.apk'; wechat.write_bytes(b'official')
            candidate = self.checker_candidate(directory, 'wechat', wechat)
            state = {'appId': 'wechat', 'worktree': str(workspace), 'report': candidate,
                     'runtime': {'root': str(device), 'socket': str(root / 'socket'), 'serial': 'emulator-5554'}}
            (directory / 'state.json').write_text(json.dumps(state))
            def probe(device_root, evidence, candidate_report):
                (runtime.smoke.EVIDENCE / 'smoke-report.json').write_text(json.dumps({
                    'status': 'FAILED', 'reason': 'No tablet login choice', 'apkSha256': sha256(wechat),
                    'runtimeDiagnostics': {'moduleLoaded': 'OBSERVED', 'compatibility': 'COMPATIBLE',
                                           'hookInstallation': 'INSTALLED_2', 'resolvedHooks': ['tablet=fixture']}}))
                (runtime.smoke.EVIDENCE / 'failure.xml').write_text('<hierarchy/>')
                return {'status': 'FAILED'}
            with ExitStack() as stack:
                stack.enter_context(patch.dict('os.environ'))
                stack.enter_context(patch.object(runtime.smoke, 'adb', return_value=MagicMock(stdout=b'installed', stderr=b'')))
                stack.enter_context(patch.object(runtime.smoke, 'configure_manager'))
                stack.enter_context(patch.object(runtime, 'probe_candidate', side_effect=probe))
                for helper_patch in self.mock_static_runtime_helpers(apk):
                    stack.enter_context(helper_patch)
                with self.assertRaisesRegex(ValueError, 'missing module injection is not supported'):
                    runtime.verify_runtime(directory)
            self.assertTrue((workspace / 'work/analysis/runtime/iteration-001/failure.xml').exists())
            self.assertEqual(sha256(apk), json.loads((directory / 'runtime-report.json').read_text())['moduleApkSha256'])

    def test_each_app_installs_the_same_module_snapshot_used_by_the_probe(self):
        scenarios = [('wechat', 'valid'), ('wechat', 'wrong-report'),
                     ('wechat', 'build-changed')]
        for app_id, scenario in scenarios:
            with self.subTest(app_id=app_id, scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory, workspace, device = root / 'task', root / 'checkout', root / 'device'
                directory.mkdir(); device.mkdir()
                apk = workspace / 'app/build/outputs/apk/debug/app-debug.apk'
                apk.parent.mkdir(parents=True); apk.write_bytes(b'adapted module')
                staged = device / 'module/app-debug.apk'
                staged.parent.mkdir(); staged.write_bytes(b'preparation module')
                preparation_sha = sha256(staged)
                candidate = device / f'{app_id}.apk'; candidate.write_bytes(b'official candidate')
                candidate_report = self.checker_candidate(directory, app_id, candidate)
                state = {'appId': app_id, 'worktree': str(workspace),
                         'report': candidate_report,
                         'runtime': {'root': str(device), 'socket': str(root / 'socket'),
                                     'serial': 'emulator-5554'}}
                (directory / 'state.json').write_text(json.dumps(state))
                installed = []
                def adb(*args, **kwargs):
                    if args[:2] == ('install', '-r') and len(args) == 3:
                        installed.append(sha256(Path(args[-1])))
                    return MagicMock(stdout=b'installed', stderr=b'')
                def probe(device_root, evidence, candidate_report):
                    self.assertEqual(sha256(apk), sha256(staged))
                    self.assertEqual([sha256(staged)], installed)
                    (runtime.smoke.EVIDENCE / 'smoke-report.json').write_text(json.dumps({
                        'status': 'RUNTIME_SMOKE_VERIFIED', 'apkSha256': sha256(candidate),
                        'moduleApkSha256': preparation_sha if scenario == 'wrong-report' else sha256(staged)}))
                    if scenario == 'build-changed':
                        apk.write_bytes(b'changed during capture')
                    return {'status': 'RUNTIME_SMOKE_VERIFIED'}
                with ExitStack() as stack:
                    stack.enter_context(patch.dict('os.environ'))
                    stack.enter_context(patch.object(runtime.smoke, 'adb', side_effect=adb))
                    stack.enter_context(patch.object(runtime.smoke, 'configure_manager'))
                    stack.enter_context(patch.object(runtime, 'probe_candidate', side_effect=probe))
                    for helper_patch in self.mock_static_runtime_helpers(apk):
                        stack.enter_context(helper_patch)
                    if scenario == 'valid':
                        runtime.verify_runtime(directory)
                    else:
                        with self.assertRaisesRegex(ValueError, 'does not match the verified module APK'):
                            runtime.verify_runtime(directory)
                report = json.loads((directory / 'runtime-report.json').read_text())
                self.assertEqual(preparation_sha if scenario == 'wrong-report' else installed[0],
                                 report['moduleApkSha256'])

    def test_rules_runtime_requires_the_shared_static_report_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, workspace, device = root / 'task', root / 'checkout', root / 'device'
            directory.mkdir(); device.mkdir()
            apk = workspace / 'app/build/outputs/apk/debug/app-debug.apk'
            apk.parent.mkdir(parents=True); apk.write_bytes(b'module')
            candidate_apk = device / 'wecom.apk'; candidate_apk.write_bytes(b'candidate')
            state = {'appId': 'wecom', 'worktree': str(workspace),
                     'report': {'appId': 'wecom', 'identity': {'apkSha256': sha256(candidate_apk)}},
                     'runtime': {'root': str(device), 'socket': str(root / 'socket'),
                                 'serial': 'emulator-5554'}}
            (directory / 'state.json').write_text(json.dumps(state))
            with patch.dict('os.environ'), patch.object(runtime.smoke, 'adb', return_value=MagicMock()) as adb, \
                 patch.object(runtime.smoke, 'configure_manager'), patch.object(runtime.smoke, 'snapshot'), \
                 patch.object(runtime.smoke, 'collect_logs'):
                with self.assertRaisesRegex(ValueError, 'shared static checker report identity'):
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
