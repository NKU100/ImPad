import json
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from contextlib import ExitStack

from scripts.ci import runtime_adaptation as runtime
from scripts.ci import candidate_selection, codex_adaptation, codex_goal
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

    def mock_static_runtime_helpers(self, apk):
        return (
            patch.object(runtime, 'prepare_module', return_value={'sha256': sha256(apk)}),
            patch.object(runtime, 'verify_module_identity'),
            patch.object(runtime, 'run_regression', side_effect=self.passed_regression),
            patch.object(runtime, 'evaluate_validation', return_value={'outcome': 'VERIFIED'}),
        )

    def test_runtime_passes_full_reports_to_the_real_validation_gate(self):
        for scenario in ('valid', 'missing-selection', 'wrong-catalog', 'empty-regression'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                task, workspace, device = root / 'task', root / 'checkout', root / 'device'
                task.mkdir(); device.mkdir()
                apk = workspace / 'app/build/outputs/apk/debug/app-debug.apk'
                apk.parent.mkdir(parents=True); apk.write_bytes(b'module')
                candidate_apk = device / 'wechat.apk'; candidate_apk.write_bytes(b'official')
                artifact_root = workspace / 'work/runtime-smoke'
                candidate = self.checker_candidate(artifact_root, 'wechat', candidate_apk)
                policy = get_policy('wechat')
                identity = dict(candidate['identity'], packageName=policy.package_name,
                                signerSha256=policy.signer_sha256, abi='arm64-v8a',
                                versionName='8.0.79', versionCode=3200)
                rule_id, strategy_id = 'wechat_tablet_login_002', 'wechat_tablet_login_behavior_002'
                plan = {'selection': 'UNIQUE', 'ruleId': rule_id, 'strategyId': strategy_id,
                        'targets': {
                            'tablet': {'descriptor': 'Lfixture/Entry;->isTablet()Z',
                                       'shape': {'parameters': [], 'returnType': 'boolean', 'isStatic': True}},
                            'login': {'descriptor': 'Lfixture/Entry;->open()V',
                                      'shape': {'parameters': [], 'returnType': 'void', 'isStatic': True}},
                        }}
                route = {'appId': 'wechat', 'candidates': [rule_id]}
                route_sha = hashlib.sha256(json.dumps(
                    route, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                module = {'sha256': sha256(apk), 'sourceTreeSha256': 'b' * 64,
                          'ruleCatalogSha256': 'a' * 64, 'routeSha256': route_sha,
                          'routeConfigurationSha256': 'c' * 64,
                          'fingerprintFileSha256': 'd' * 64, 'dependencyLockSha256': 'e' * 64,
                          'route': route}
                static_path = artifact_root / candidate['staticReport']
                static = json.loads(static_path.read_text())
                static.update(identity=identity, analysisScope='STATIC_ONLY',
                              routeCandidates=[rule_id],
                              selection={'status': 'SELECTED', 'selectedRuleId': rule_id,
                                         'strategyId': strategy_id, 'targets': plan['targets']},
                              ruleCatalog={'schemaVersion': 1, 'appId': 'wechat',
                                           'ruleCatalogSha256': 'a' * 64,
                                           'definitions': [{'ruleId': rule_id, 'strategyId': strategy_id,
                                                            'requiredTargetIds': ['tablet', 'login']}],
                                           'bindings': [{'ruleId': rule_id, 'min': 3200, 'max': None}],
                                           'strategyIds': [strategy_id]},
                              executionIdentity={key: module[key] for key in (
                                  'ruleCatalogSha256', 'sourceTreeSha256', 'routeConfigurationSha256',
                                  'fingerprintFileSha256', 'dependencyLockSha256')})
                if scenario == 'missing-selection':
                    static.pop('selection')
                elif scenario == 'wrong-catalog':
                    static['ruleCatalog']['ruleCatalogSha256'] = 'f' * 64
                static_path.write_text(json.dumps(static))
                candidate.update(identity=identity, expectedPlan=plan,
                                 sourceUrl=policy.official_apk_prefix + 'fixture.apk',
                                 staticReportSha256=sha256(static_path), ruleCatalogSha256='a' * 64,
                                 route=dict(route, samplingMode='DIRECT'), routeSha256=route_sha)
                (artifact_root / 'candidate-report.json').write_text(json.dumps(candidate))
                (task / 'state.json').write_text(json.dumps({
                    'appId': 'wechat', 'worktree': str(workspace), 'report': candidate,
                    'runtime': {'root': str(device), 'socket': str(root / 'socket'),
                                'serial': 'emulator-5554'}}))

                def regression(app_id, reports, **kwargs):
                    self.assertEqual(workspace, kwargs.get('repository'))
                    rows = [] if scenario == 'empty-regression' else [{'status': 'PASSED'}]
                    reports.mkdir(parents=True)
                    (reports / 'results.json').write_text(json.dumps(rows))
                    return rows

                def probe(device_root, evidence, candidate_report):
                    raw = evidence / 'logcat.txt'; raw.write_text('fixture runtime evidence')
                    report = {'status': 'RUNTIME_SMOKE_VERIFIED', 'appId': 'wechat',
                              'packageName': policy.package_name, 'abi': 'arm64-v8a',
                              'apkSha256': identity['apkSha256'], 'moduleApkSha256': module['sha256'],
                              'moduleSourceTreeSha256': module['sourceTreeSha256'],
                              'ruleCatalogSha256': module['ruleCatalogSha256'],
                              'routeSha256': route_sha, 'dependencyLockSha256': module['dependencyLockSha256'],
                              'evidence': [{'path': raw.name, 'sha256': sha256(raw)}]}
                    (evidence / 'smoke-report.json').write_text(json.dumps(report))
                    return report

                with patch.dict('os.environ'), \
                     patch.object(runtime, 'prepare_module', return_value=module), \
                     patch.object(runtime, 'verify_module_identity'), \
                     patch.object(runtime, 'run_regression', side_effect=regression), \
                     patch.object(runtime, 'probe_candidate', side_effect=probe), \
                     patch.object(runtime.smoke, 'snapshot'), patch.object(runtime.smoke, 'collect_logs'), \
                     patch.object(runtime.smoke, 'adb', return_value=MagicMock(stdout=b'ok', stderr=b'')):
                    if scenario == 'valid':
                        runtime.verify_runtime(task)
                        validation = json.loads((task / 'validation-report.json').read_text())
                        self.assertEqual('VERIFIED', validation['outcome'])
                        self.assertEqual(static, validation['static'])
                        self.assertEqual(sha256(device / 'regression/results.json'),
                                         validation['stageEvidence']['regression']['sha256'])
                        self.assertEqual(sha256(device / 'iteration-001/smoke-report.json'),
                                         validation['stageEvidence']['runtime']['sha256'])
                    else:
                        expected_error = {
                            'missing-selection': 'Shared static check did not pass',
                            'wrong-catalog': 'Static plan does not match',
                            'empty-regression': 'Fixed acceptance regression failed',
                        }[scenario]
                        with self.assertRaisesRegex(ValueError, expected_error):
                            runtime.verify_runtime(task)

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
            candidate = self.checker_candidate(workspace / 'work/runtime-smoke', 'wechat', wechat)
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
                candidate_report = self.checker_candidate(workspace / 'work/runtime-smoke', app_id, candidate)
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
            original_report = {'appId': 'wecom', 'identity': {'apkSha256': sha256(candidate_apk)}}
            state = {'appId': 'wecom', 'worktree': str(workspace), 'report': original_report,
                     'runtime': {'root': str(device), 'socket': str(root / 'socket'),
                                 'serial': 'emulator-5554'}}
            artifact = workspace / 'work/runtime-smoke'
            artifact.mkdir(parents=True)
            (artifact / 'candidate-report.json').write_text(json.dumps({
                'schemaVersion': 1, 'appId': 'wecom', 'status': 'PREPARED',
                'identity': original_report['identity']}))
            (directory / 'state.json').write_text(json.dumps(state))
            with patch.dict('os.environ'), patch.object(runtime.smoke, 'adb', return_value=MagicMock()) as adb, \
                 patch.object(runtime.smoke, 'configure_manager'), patch.object(runtime.smoke, 'snapshot'), \
                 patch.object(runtime.smoke, 'collect_logs'):
                with self.assertRaisesRegex(ValueError, 'shared static checker report identity'):
                    runtime.verify_runtime(directory)
            adb.assert_not_called()

    def test_runtime_verifies_the_refreshed_candidate_artifact_not_initial_state_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, workspace, device = root / 'task', root / 'checkout', root / 'artifact'
            directory.mkdir(); workspace.mkdir(); device.mkdir()
            apk = workspace / 'app/build/outputs/apk/debug/app-debug.apk'
            apk.parent.mkdir(parents=True); apk.write_bytes(b'adapted module')
            candidate_apk = device / 'wechat.apk'; candidate_apk.write_bytes(b'exact candidate')
            device.mkdir(parents=True, exist_ok=True)
            prepared = self.checker_candidate(workspace / 'work/runtime-smoke', 'wechat', candidate_apk)
            original = dict(prepared, status='INCOMPATIBLE')
            original.pop('expectedPlan')
            state = {'appId': 'wechat', 'worktree': str(workspace), 'report': original,
                     'runtime': {'root': str(device), 'socket': str(root / 'socket'),
                                 'serial': 'emulator-5554'}}
            (directory / 'state.json').write_text(json.dumps(state))

            def probe(device_root, evidence, candidate_report):
                self.assertEqual('PREPARED', candidate_report['status'])
                self.assertEqual(prepared['staticReport'], candidate_report['staticReport'])
                (runtime.smoke.EVIDENCE / 'smoke-report.json').write_text(json.dumps({
                    'status': 'RUNTIME_SMOKE_VERIFIED', 'apkSha256': sha256(candidate_apk),
                    'moduleApkSha256': sha256(apk)}))
                return {'status': 'RUNTIME_SMOKE_VERIFIED'}

            with ExitStack() as stack:
                stack.enter_context(patch.dict('os.environ'))
                stack.enter_context(patch.object(runtime.smoke, 'adb', return_value=MagicMock(stdout=b'ok', stderr=b'')))
                stack.enter_context(patch.object(runtime.smoke, 'configure_manager'))
                stack.enter_context(patch.object(runtime, 'probe_candidate', side_effect=probe))
                for helper_patch in self.mock_static_runtime_helpers(apk):
                    stack.enter_context(helper_patch)
                runtime.verify_runtime(directory)

    def test_goal_verification_refreshes_candidate_and_consumes_relative_artifact_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, repository = root / 'task', root / 'repository'
            task.mkdir(); repository.mkdir()
            subprocess.run(['git', 'init', '-q'], cwd=repository, check=True)
            (repository / '.gitignore').write_text('work/\n.cache/\napp/build/\n')
            subprocess.run(['git', 'add', '.gitignore'], cwd=repository, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'fixture'], cwd=repository, check=True)
            base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository, text=True).strip()

            policy = get_policy('wechat')
            candidate_bytes = b'official candidate fixture'
            candidate_digest = hashlib.sha256(candidate_bytes).hexdigest()
            candidate_apk = repository / 'work/apks' / f'candidate-{candidate_digest}.apk'
            candidate_apk.parent.mkdir(parents=True)
            candidate_apk.write_bytes(candidate_bytes)
            runtime_root = repository / 'work/runtime-smoke'
            runtime_root.mkdir(parents=True)
            device_root = task / 'device'
            device_root.mkdir()
            (device_root / 'wechat.apk').write_bytes(candidate_bytes)
            identity = {
                'packageName': policy.package_name, 'versionName': '8.0.79', 'versionCode': 3200,
                'abi': policy.supported_abis[0], 'apkSha256': candidate_digest,
                'signerSha256': policy.signer_sha256,
            }
            original = {
                'schemaVersion': 1, 'appId': 'wechat', 'status': 'INCOMPATIBLE',
                'identity': identity,
                'sourceUrl': policy.official_apk_prefix + 'fixture.apk',
            }
            state = {
                'appId': 'wechat', 'base': base, 'agent_base': base,
                'worktree': str(repository), 'report': original,
                'existing_tests': {}, 'baseBindings': [{'ruleId': 'legacy', 'min': 3040, 'max': None}],
                'acceptanceRecords': [], 'acceptanceSnapshotSha256': 'f' * 64,
                'apks': [(3200, str(candidate_apk), candidate_digest)],
                'runtime': {'root': str(device_root), 'socket': str(root / 'private-adb.sock'),
                            'serial': 'emulator-5554'},
            }
            (task / 'state.json').write_text(json.dumps(state))
            (repository / 'work/analysis').mkdir(parents=True)
            (repository / 'work/analysis/adaptation-summary.json').write_text(json.dumps({
                'completed': [], 'blockers': [], 'nextActions': [], 'questions': [],
            }))

            bindings = [{'ruleId': 'legacy', 'min': 3040, 'max': 3040},
                        {'ruleId': 'wechat_tablet_login_002', 'min': 3200, 'max': None}]
            static = {
                'appId': 'wechat', 'analysisScope': 'STATIC_ONLY', 'identity': identity,
                'selection': {'status': 'SELECTED', 'selectedRuleId': 'wechat_tablet_login_002',
                              'strategyId': 'wechat_tablet_login_behavior_001',
                              'targets': {'tablet': {'descriptor': 'Lfixture/Entry;->open()V'}}},
                'routeCandidates': ['Lfixture/Entry;->open()V'],
                'ruleCatalogSha256': 'a' * 64, 'ruleCatalog': {'bindings': bindings},
                'executionIdentity': {'ruleCatalogSha256': 'a' * 64},
                'inputHashes': [{'path': str(candidate_apk), 'sha256': candidate_digest}],
            }

            def checker(app_id, apk, report_path, repository=None):
                self.assertEqual('wechat', app_id)
                self.assertEqual(candidate_digest, sha256(apk))
                checked = dict(static, inputHashes=[{'path': str(apk), 'sha256': candidate_digest}])
                report_path.write_text(json.dumps(checked))
                return checked

            module_apk = repository / 'app/build/outputs/apk/debug/app-debug.apk'
            module_apk.parent.mkdir(parents=True)
            module_apk.write_bytes(b'built module fixture')

            def build(*args, **kwargs):
                self.assertTrue((repository / 'work/runtime-smoke/static-check.json').exists())

            def probe(device_root, evidence, candidate_report):
                self.assertEqual('PREPARED', candidate_report['status'])
                self.assertFalse(Path(candidate_report['staticReport']).is_absolute())
                self.assertFalse(Path(candidate_report['apkFiles'][0]['path']).is_absolute())
                self.assertFalse(Path(json.loads((runtime_root / candidate_report['staticReport']).read_text())
                                     ['inputHashes'][0]['path']).is_absolute())
                (runtime.smoke.EVIDENCE / 'smoke-report.json').write_text(json.dumps({
                    'status': 'RUNTIME_SMOKE_VERIFIED', 'apkSha256': candidate_digest,
                    'moduleApkSha256': sha256(module_apk),
                }))
                return {'status': 'RUNTIME_SMOKE_VERIFIED'}

            with ExitStack() as stack:
                stack.enter_context(patch.dict('os.environ'))
                stack.enter_context(patch.object(candidate_selection, '_run_rules_checker', side_effect=checker))
                stack.enter_context(patch.object(codex_adaptation, 'validate_rule_changes'))
                stack.enter_context(patch('scripts.ci.acceptance.propose_forward_binding', return_value=bindings))
                stack.enter_context(patch.object(codex_adaptation, 'run_regression', return_value=[{'status': 'PASSED'}]))
                stack.enter_context(patch.object(candidate_selection, 'write_local_runtime_assets'))
                stack.enter_context(patch.object(codex_adaptation, 'run_bounded', side_effect=build))
                stack.enter_context(patch.object(candidate_selection, 'prepare_module',
                                                 side_effect=lambda path, *_: {'sha256': sha256(path)}))
                stack.enter_context(patch.object(runtime, 'prepare_module',
                                                 side_effect=lambda path, *_: {'sha256': sha256(path)}))
                stack.enter_context(patch.object(runtime, 'verify_module_identity'))
                stack.enter_context(patch.object(runtime, 'run_regression', side_effect=self.passed_regression))
                stack.enter_context(patch.object(runtime, 'evaluate_validation', return_value={'outcome': 'VERIFIED'}))
                stack.enter_context(patch.object(runtime.smoke, 'adb', return_value=MagicMock(stdout=b'ok', stderr=b'')))
                stack.enter_context(patch.object(runtime.smoke, 'configure_manager'))
                stack.enter_context(patch.object(runtime, 'probe_candidate', side_effect=probe))
                codex_goal.verify(task)

            refreshed = json.loads((runtime_root / 'candidate-report.json').read_text())
            final = json.loads((task / 'candidate-report.json').read_text())
            persisted = json.loads((task / 'state.json').read_text())
            self.assertEqual('PREPARED', refreshed['status'])
            self.assertFalse(Path(refreshed['staticReport']).is_absolute())
            self.assertFalse(Path(refreshed['apkFiles'][0]['path']).is_absolute())
            self.assertEqual('STATIC_VERIFIED_PENDING_RUNTIME', final['status'])
            self.assertEqual('INCOMPATIBLE', persisted['report']['status'])
            self.assertEqual(identity, persisted['report']['identity'])
            self.assertEqual('RUNTIME_SMOKE_VERIFIED',
                             json.loads((task / 'runtime-report.json').read_text())['status'])

    def test_runtime_candidate_artifact_references_cannot_escape_candidate_root(self):
        cases = ('static-report', 'candidate-apk', 'static-input', 'candidate-report-symlink',
                 'candidate-apk-symlink', 'absolute-static-report')
        for case in cases:
            with self.subTest(reference=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory, workspace, artifact = root / 'task', root / 'checkout', root / 'artifact'
                directory.mkdir(); workspace.mkdir(); artifact.mkdir()
                module = workspace / 'app/build/outputs/apk/debug/app-debug.apk'
                module.parent.mkdir(parents=True); module.write_bytes(b'module')
                candidate_apk = artifact / 'wechat.apk'; candidate_apk.write_bytes(b'candidate')
                candidate = self.checker_candidate(workspace / 'work/runtime-smoke', 'wechat', candidate_apk)
                if case == 'static-report':
                    candidate['staticReport'] = '../outside-static.json'
                    message = 'Static report path escapes the candidate artifact root'
                elif case == 'candidate-apk':
                    candidate['apkFiles'][0]['path'] = '../outside.apk'
                    message = 'Candidate APK path escapes the candidate artifact root'
                elif case == 'candidate-apk-symlink':
                    outside = root / 'outside.apk'; outside.write_bytes(b'outside candidate')
                    (workspace / 'work/runtime-smoke/escape.apk').symlink_to(outside)
                    candidate['apkFiles'][0]['path'] = 'escape.apk'
                    message = 'Candidate APK path escapes the candidate artifact root'
                elif case == 'candidate-report-symlink':
                    outside = root / 'outside-report.json'
                    outside.write_text(json.dumps(candidate))
                    candidate_report = workspace / 'work/runtime-smoke/candidate-report.json'
                    candidate_report.unlink()
                    candidate_report.symlink_to(outside)
                    message = 'Prepared candidate report escapes the candidate artifact root'
                elif case == 'absolute-static-report':
                    candidate['staticReport'] = str((root / 'outside-static.json').resolve())
                    message = 'Static report path must be relative to the candidate artifact root'
                else:
                    static_path = workspace / 'work/runtime-smoke' / candidate['staticReport']
                    static = json.loads(static_path.read_text())
                    static['inputHashes'][0]['path'] = '../outside-input.apk'
                    static_path.write_text(json.dumps(static))
                    candidate['staticReportSha256'] = sha256(static_path)
                    message = 'Static checker APK input path escapes the candidate artifact root'
                candidate_artifact = workspace / 'work/runtime-smoke'
                if case != 'candidate-report-symlink':
                    (candidate_artifact / 'candidate-report.json').write_text(json.dumps(candidate))
                original = dict(candidate, status='INCOMPATIBLE')
                state = {'appId': 'wechat', 'worktree': str(workspace), 'report': original,
                         'runtime': {'root': str(artifact), 'socket': str(root / 'socket'),
                                     'serial': 'emulator-5554'}}
                (directory / 'state.json').write_text(json.dumps(state))
                with patch.dict('os.environ'), patch.object(runtime.smoke, 'snapshot'), \
                     patch.object(runtime.smoke, 'collect_logs'), patch.object(runtime.smoke, 'adb') as adb:
                    with self.assertRaisesRegex(ValueError, message):
                        runtime.verify_runtime(directory)
                adb.assert_not_called()

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
