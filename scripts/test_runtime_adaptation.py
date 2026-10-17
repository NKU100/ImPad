import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch, MagicMock

from scripts.ci import runtime_adaptation as runtime
from scripts.ci.codex_adaptation import sha256
from scripts.ci.app_policy import POLICIES, get_policy


class RuntimeAdaptationTest(unittest.TestCase):
    def setUp(self):
        for name in ['ROOT', 'EVIDENCE', 'stage', 'APP_ID', 'APP_POLICY', 'APP_PACKAGE', 'UI_STRATEGY']:
            original = getattr(runtime.smoke, name)
            self.addCleanup(setattr, runtime.smoke, name, original)

    def test_initializer_refuses_a_physical_or_additional_device_before_setup(self):
        for devices in ['List of devices attached\n127.0.0.1:16556\tdevice\n',
                        'List of devices attached\nphone\tdevice\n',
                        'List of devices attached\nemulator-5554\tdevice\nphone\tdevice\n']:
            with patch.object(runtime.subprocess, 'check_output', return_value=devices), patch.object(runtime, 'AppSmoke') as setup:
                with self.assertRaisesRegex(RuntimeError, 'exactly one disposable'):
                    runtime.initialize(Path('/unused'), Path('/unused'), 'emulator-5554')
                setup.assert_not_called()

    def test_initializer_uses_the_explicit_policy_apk_and_package(self):
        policy = replace(get_policy('wechat'), app_id='fixture', package_name='org.example.fixture',
                         supported_abis=('arm64-v8a',), runtime_baseline='FIXTURE_BASELINE',
                         runtime_qr_page='FixtureQrPage')
        with tempfile.TemporaryDirectory() as temporary, patch.dict(POLICIES, {'fixture': policy}):
            root = Path(temporary)
            directory = root / 'task'; directory.mkdir()
            worktree = root / 'checkout'; worktree.mkdir()
            runtime_root = root / 'runtime'; runtime_root.mkdir()
            (runtime_root / 'fixture.apk').write_bytes(b'app specific apk')
            report = {'appId': 'fixture', 'identity': {'apkSha256': sha256(runtime_root / 'fixture.apk')}}
            state = {'appId': 'fixture', 'report': report, 'worktree': str(worktree)}
            state_path = directory / 'state.json'; state_path.write_text(json.dumps(state))
            listing = 'List of devices attached\n127.0.0.1:16555\tdevice\n'
            with patch.object(runtime.subprocess, 'check_output', side_effect=[listing, listing]), \
                 patch.object(runtime.subprocess, 'run') as run_command, \
                 patch.object(runtime, 'query_target'), patch.object(runtime, 'AppSmoke') as app, \
                 patch.object(runtime.smoke, 'configure_manager'), patch.object(runtime.smoke, 'adb'), \
                 patch.object(runtime.smoke, 'mobile_input', return_value='<hierarchy/>'):
                runtime.initialize(directory, runtime_root, '127.0.0.1:16555')
            app.return_value.setup.assert_called_once()
            self.assertEqual('fixture', runtime.smoke.APP_ID)
            self.assertEqual(4, run_command.call_count)
            device_instructions = json.loads((worktree / 'work/analysis/runtime-device.json').read_text())
            self.assertEqual('fixture', device_instructions['appId'])
            self.assertIn('org.example.fixture', device_instructions['instructions'])

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
            state = {'worktree': str(workspace), 'report': {'identity': {'apkSha256': sha256(wechat)}},
                     'runtime': {'root': str(device), 'socket': str(root / 'socket'), 'serial': 'emulator-5554'}}
            (directory / 'state.json').write_text(json.dumps(state))
            def probe(device_root, evidence, candidate_report):
                (runtime.smoke.EVIDENCE / 'smoke-report.json').write_text(json.dumps({
                    'status': 'FAILED', 'reason': 'No tablet login choice', 'apkSha256': sha256(wechat),
                    'runtimeDiagnostics': {'moduleLoaded': 'OBSERVED', 'compatibility': 'COMPATIBLE',
                                           'hookInstallation': 'INSTALLED_2', 'resolvedHooks': ['tablet=fixture']}}))
                (runtime.smoke.EVIDENCE / 'failure.xml').write_text('<hierarchy/>')
                return {'status': 'FAILED'}
            with patch.dict('os.environ'), patch.object(runtime.smoke, 'adb', return_value=MagicMock(stdout=b'installed', stderr=b'')), patch.object(runtime.smoke, 'configure_manager'), patch.object(runtime, 'probe_candidate', side_effect=probe):
                with self.assertRaisesRegex(ValueError, 'missing module injection is not supported'):
                    runtime.verify_runtime(directory)
            self.assertTrue((workspace / 'work/analysis/runtime/iteration-001/failure.xml').exists())
            self.assertEqual(sha256(apk), json.loads((directory / 'runtime-report.json').read_text())['moduleApkSha256'])

    def test_each_app_installs_the_same_module_snapshot_used_by_the_probe(self):
        scenarios = [('wechat', 'valid'), ('wecom', 'valid'), ('qq', 'valid'),
                     ('qq', 'wrong-report'), ('qq', 'build-changed')]
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
                state = {'appId': app_id, 'worktree': str(workspace),
                         'report': {'identity': {'apkSha256': sha256(candidate)}},
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
                with patch.dict('os.environ'), patch.object(runtime.smoke, 'adb', side_effect=adb), \
                     patch.object(runtime.smoke, 'configure_manager'), \
                     patch.object(runtime, 'probe_candidate', side_effect=probe):
                    if scenario == 'valid':
                        runtime.verify_runtime(directory)
                    else:
                        with self.assertRaisesRegex(ValueError, 'does not match the verified module APK'):
                            runtime.verify_runtime(directory)
                report = json.loads((directory / 'runtime-report.json').read_text())
                self.assertEqual(preparation_sha if scenario == 'wrong-report' else installed[0],
                                 report['moduleApkSha256'])

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
