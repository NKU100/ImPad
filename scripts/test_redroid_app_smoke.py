import json
import hashlib
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from scripts.ci import redroid_app_smoke as app
from scripts.ci import redroid_prepare_apps as prepare
from scripts.ci import runtime_task
from scripts.test_semantic_hook_evidence import lifecycle_fixture


class ReDroidApplicationSmokeTest(unittest.TestCase):
    @staticmethod
    def _qq_execute_identity():
        return {
            'packageName': 'com.tencent.mobileqq', 'versionName': '9.3.70',
            'versionCode': 16410, 'abi': 'arm64-v8a',
            'apkSha256': 'a' * 64, 'signerSha256': 'b' * 64,
        }

    @staticmethod
    def _qq_execute_fixture(root, candidate=None):
        output = root / 'output'
        output.mkdir()
        identity = ReDroidApplicationSmokeTest._qq_execute_identity()
        (root / 'inputs.json').write_text(json.dumps({'qq': {'identity': identity}}))
        if candidate is not None:
            (root / 'candidate-report.json').write_text(json.dumps(candidate))
        instance = object.__new__(app.AppSmoke)
        instance.root = root.resolve()
        instance.probe = SimpleNamespace(output=output)
        instance.inputs = {'qq': {'identity': identity}}
        instance.module_hash = 'c' * 64
        instance.module_identity = {
            'sha256': instance.module_hash,
            'sourceTreeSha256': 'd' * 64,
            'ruleCatalogSha256': 'e' * 64,
            'recognitionSha256': 'f' * 64,
            'dependencyLockSha256': '1' * 64,
        }
        instance.memory_stop = threading.Event()
        instance.memory = []
        instance.memory_errors = []
        return instance, identity

    def test_execute_passes_exact_schema2_qq_candidate_to_real_qq_signature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = self._qq_execute_identity()
            instance, _ = self._qq_execute_fixture(root, {
                'schemaVersion': 2, 'appId': 'qq', 'status': 'PREPARED',
                'identity': identity,
            })
            candidate = json.loads((root / 'candidate-report.json').read_text())
            runtime_report = {'appId': 'qq', 'status': 'RUNTIME_SMOKE_VERIFIED', 'evidence': []}
            thread = SimpleNamespace(start=lambda: None, join=unittest.mock.Mock())
            with patch.object(instance, 'setup') as setup, \
                    patch.object(instance, 'monitor') as monitor, \
                    patch.object(app.threading, 'Thread', return_value=thread), \
                    patch.object(app.AppSmoke, 'qq', autospec=True,
                                 return_value=runtime_report) as qq, \
                    patch.object(app.smoke, 'adb', side_effect=AssertionError('ADB accessed')):
                instance.execute()

            setup.assert_called_once_with()
            monitor.assert_not_called()
            qq.assert_called_once_with(instance, instance.inputs['qq'], candidate)
            thread.join.assert_called_once_with(timeout=20)
            self.assertTrue(instance.memory_stop.is_set())
            smoke_report = json.loads((root / 'output/evidence/smoke-report.json').read_text())
            self.assertEqual('RUNTIME_SMOKE_VERIFIED', smoke_report['status'])
            self.assertEqual('qq', smoke_report['appId'])

    def test_execute_rejects_missing_or_mismatched_qq_candidate_and_saves_failclosed_evidence(self):
        qq_identity = self._qq_execute_identity()
        other_identity = {
            'packageName': 'com.tencent.mm', 'versionName': '8.0.80',
            'versionCode': 3220, 'abi': 'arm64-v8a',
            'apkSha256': '9' * 64, 'signerSha256': '8' * 64,
        }
        bad_candidates = (
            None,
            {'schemaVersion': 2, 'appId': 'wechat', 'status': 'PREPARED',
             'identity': other_identity},
            {'schemaVersion': 2, 'appId': 'qq', 'status': 'PREPARED',
             'identity': dict(qq_identity, apkSha256='9' * 64)},
            {'schemaVersion': 1, 'appId': 'qq', 'status': 'PREPARED',
             'identity': qq_identity},
        )
        for candidate in bad_candidates:
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                instance, _ = self._qq_execute_fixture(root, candidate)
                thread = SimpleNamespace(start=lambda: None, join=unittest.mock.Mock())
                with patch.object(instance, 'setup') as setup, \
                        patch.object(instance, 'monitor'), \
                        patch.object(app.threading, 'Thread', return_value=thread), \
                        patch.object(app.AppSmoke, 'qq', autospec=True) as qq, \
                        patch.object(app.smoke, 'adb', side_effect=AssertionError('ADB accessed')):
                    with self.assertRaises(ValueError):
                        instance.execute()

                setup.assert_called_once_with()
                qq.assert_not_called()
                thread.join.assert_called_once_with(timeout=20)
                self.assertTrue(instance.memory_stop.is_set())
                app_report = json.loads((root / 'output/app-reports.json').read_text())
                smoke_report = json.loads((root / 'output/evidence/smoke-report.json').read_text())
                self.assertEqual('qq', app_report[0]['appId'])
                self.assertEqual('ACCEPTANCE_UNAVAILABLE', app_report[0]['status'])
                self.assertEqual('qq', smoke_report['appId'])
                self.assertEqual('ACCEPTANCE_UNAVAILABLE', smoke_report['status'])

    def test_wecom_summary_only_fixture_is_rejected_without_production_raw_captures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_dir = root / 'apps/wecom'
            evidence_dir.mkdir(parents=True)
            files = {
                'apps/wecom/logcat.txt': evidence_dir / 'logcat.txt',
                'apps/wecom/screen.xml': evidence_dir / 'screen.xml',
            }
            files['apps/wecom/logcat.txt'].write_text('captured WeCom logcat')
            files['apps/wecom/screen.xml'].write_text('<hierarchy/>')
            smoke = {'evidence': [
                {'path': path, 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
                for path, file in files.items()
            ]}
            candidate = {'identity': {'packageName': 'com.tencent.wework'}}

            with self.assertRaisesRegex(
                    ValueError, 'missing required UI, process, module or manager snapshots'):
                runtime_task._validate_wecom_raw_evidence(smoke, root, candidate, {})

    def test_qq_lifecycle_requires_native_qfix_hook_and_request_order_from_same_process(self):
        logs = '\n'.join([
            '10-19 12:00:00.001 123 456 I ImPad: rule-runtime event=qfix_ready elapsedRealtimeNs=100 contextPackage=com.tencent.mobileqq loaderClass=dalvik.system.PathClassLoader',
            '10-19 12:00:00.002 123 456 I ImPad: rule-runtime event=native_loaded elapsedRealtimeNs=200 apiSha256=' + 'a' * 64 + ' nativeSha256=' + 'b' * 64,
            '10-19 12:00:00.003 123 456 I ImPad: rule-runtime event=hook_installed elapsedRealtimeNs=300 ruleId=qq_device_type_001 strategyId=qq_tablet_001 hookCount=1 processName=com.tencent.mobileqq:MSF',
        ])
        evidence = app.extract_qq_lifecycle_evidence(logs, {123}, [400])
        self.assertEqual('LOADED', evidence['nativeEvidence']['status'])
        self.assertEqual('READY', evidence['qfixEvidence']['status'])
        self.assertEqual({'qfixReadyBeforeNative': True, 'hookInstalledBeforeRequest': True},
                         evidence['timingEvidence'])

    def test_qq_lifecycle_does_not_infer_missing_events(self):
        evidence = app.extract_qq_lifecycle_evidence('', {123}, [400])
        self.assertEqual('NOT_OBSERVED', evidence['nativeEvidence']['status'])
        self.assertFalse(evidence['timingEvidence']['hookInstalledBeforeRequest'])

    def test_qq_semantic_lifecycle_binds_current_msf_and_complete_plan_before_requests(self):
        candidate, module, processes, logs, installed, _ = lifecycle_fixture('qq')
        diagnostics, evidence = app.extract_qq_semantic_lifecycle_evidence(
            logs, {3100}, [400, 500], candidate, module, processes, installed)
        self.assertEqual('INSTALLED_1', diagnostics['hookInstallation'])
        self.assertEqual('READY', evidence['qfixEvidence']['status'])
        self.assertTrue(evidence['timingEvidence']['qfixReadyBeforeNative'])
        self.assertTrue(evidence['timingEvidence']['hookInstalledBeforeRequest'])

    def test_qq_semantic_lifecycle_rejects_wrong_pid_missing_plan_stale_process_and_multiple_owners(self):
        candidate, module, processes, logs, _, _ = lifecycle_fixture('qq')
        cases = (
            (logs, {9999}, processes),
            ('\n'.join(line for line in logs.splitlines() if 'semantic_plan' not in line), {3100}, processes),
            (logs.replace('(com.tencent.mobileqq:MSF)', '(com.tencent.mobileqq:worker)'), {3100}, processes),
            (logs, {3100, 3200}, processes + '3200 com.tencent.mobileqq:MSF\n'),
        )
        for scoped, owners, process_text in cases:
            with self.subTest(owners=owners, process_text=process_text), self.assertRaises(ValueError):
                app.extract_qq_semantic_lifecycle_evidence(
                    scoped, owners, [400], candidate, module, process_text)

    def test_qq_semantic_lifecycle_requires_positive_request_times_and_hook_precedes_all(self):
        candidate, module, processes, logs, _, _ = lifecycle_fixture('qq')
        for request_times in ([], [0, -1], [250, 400]):
            with self.subTest(request_times=request_times), self.assertRaises(ValueError):
                app.extract_qq_semantic_lifecycle_evidence(
                    logs, {3100}, request_times, candidate, module, processes)

    def test_qq_process_snapshot_uses_actual_ps_output_and_requires_one_current_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence_dir = Path(temporary)
            expected = 'PID NAME\n3100 com.tencent.mobileqq:MSF\n'
            fake_smoke = SimpleNamespace(su=lambda command: SimpleNamespace(stdout=expected.encode()))
            process_text = app.capture_qq_process_snapshot(fake_smoke, evidence_dir, {3100})
            self.assertEqual(expected, process_text)
            self.assertEqual(expected, (evidence_dir / 'processes-enabled.txt').read_text())
            for bad_output, owners in ((expected, {3101}),
                                       (expected + '3200 com.tencent.mobileqq:MSF\n', {3100}),
                                       ('PID NAME\n', {3100})):
                fake_smoke.su = lambda command, output=bad_output: SimpleNamespace(stdout=output.encode())
                with self.subTest(output=bad_output, owners=owners), self.assertRaises(ValueError):
                    app.capture_qq_process_snapshot(fake_smoke, evidence_dir, owners)

    def test_qq_compatibility_and_hook_installation_must_share_a_captured_owner(self):
        logs = '\n'.join([
            'x x 123 x I ImPad: status=COMPATIBLE',
            'x x 456 x I ImPad: installed 1 QQ hooks',
        ])
        self.assertFalse(app.qq_compatible_hook_installed(logs, {123, 456}))
        logs += '\nx x 123 x I ImPad: installed 1 QQ hooks'
        self.assertTrue(app.qq_compatible_hook_installed(logs, {123}))

    def test_production_qq_smoke_does_not_accept_legacy_markers_without_semantic_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output'
            (root / 'qq.apk').write_bytes(b'qq')
            instance = object.__new__(app.AppSmoke)
            instance.root = root
            instance.probe = SimpleNamespace(output=output)
            instance.inputs = {'qq': {
                'identity': {'versionName': '9.3.1', 'apkSha256': 'a' * 64},
                'requestIdentityExpectation': {
                    'contract': 'QQ_SSO_REQUEST_IDENTITY_V1', 'apkSha256': 'a' * 64,
                    'phoneAppId': 10, 'tabletAppId': 20}}}
            instance.module_hash = 'b' * 64
            instance.module_identity = {'dexkit': {}}
            from scripts.test_qq_requests import proof
            capture_proof = proof('a' * 64, 'b' * 64)
            disabled = capture_proof['disabled']
            enabled = capture_proof['enabled']

            def capture(_instance, active, _profile):
                if active:
                    evidence = output / 'apps' / 'qq'
                    evidence.mkdir(parents=True, exist_ok=True)
                    (evidence / 'processes-enabled.txt').write_text(
                        'PID NAME\n123 com.tencent.mobileqq:MSF\n')
                    (evidence / 'logcat-enabled.txt').write_text(
                        '10-19 12:00:00.001 123 456 I ImPad: status=COMPATIBLE\n'
                        '10-19 12:00:00.002 123 456 I ImPad: installed 1 QQ hooks\n')
                return enabled if active else disabled

            with patch.object(app.smoke, 'configure_app_id'), \
                    patch.object(app, 'checked_digest'), \
                    patch.object(app.smoke, 'adb', return_value=SimpleNamespace(stdout=b'', stderr=b'', returncode=0)), \
                    patch.object(app.smoke, 'mobile_input'), \
                    patch.object(app.smoke, 'hook_diagnostics', return_value={}), \
                    patch.object(app.smoke, 'save'), \
                    patch.object(app.smoke, 'snapshot'), \
                    patch.object(app.runtime_probe, 'manager_state', return_value={'scopeVerified': True}), \
                    patch.object(app.runtime_probe, 'installed_identity',
                                 return_value={'baseApkPath': '/data/app/impad/base.apk'}), \
                    patch.object(app.AppSmoke, 'capture', autospec=True, side_effect=capture):
                report = instance.run_candidate({
                    'appId': 'qq',
                    'identity': {'packageName': 'com.tencent.mobileqq', 'abi': 'arm64-v8a',
                                 'apkSha256': 'a' * 64},
                })
                written_report = json.loads((output / 'evidence/smoke-report.json').read_text())

        self.assertEqual('ACCEPTANCE_UNAVAILABLE', report['status'])
        self.assertEqual('ValueError: Semantic lifecycle candidate must use schema version 2',
                         report['reason'])
        self.assertEqual('REQUEST_APP_ID_CONTRAST', report['acceptanceKind'])
        self.assertEqual(capture_proof, report['requestIdentityEvidence'])
        self.assertEqual(report, written_report)

    def test_successful_qq_report_saves_both_semantic_summaries_and_hashed_process_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output'
            (root / 'qq.apk').write_bytes(b'qq')
            candidate, semantic_module, process_text, logs, installed, _ = lifecycle_fixture(
                'qq', apk_sha='a' * 64)
            module = dict(semantic_module, sha256='b' * 64, sourceTreeSha256='c' * 64,
                          ruleCatalogSha256=candidate['ruleCatalogSha256'],
                          recognitionSha256='d' * 64, dependencyLockSha256='e' * 64)
            instance = object.__new__(app.AppSmoke)
            instance.root = root
            instance.probe = SimpleNamespace(output=output)
            instance.inputs = {'qq': {'identity': dict(candidate['identity'], versionName='9.3.1'),
                                      'requestIdentityExpectation': {
                                          'contract': 'QQ_SSO_REQUEST_IDENTITY_V1',
                                          'apkSha256': 'a' * 64, 'phoneAppId': 10, 'tabletAppId': 20}}}
            instance.module_hash = 'b' * 64
            instance.module_identity = module
            from scripts.test_qq_requests import proof
            pair = proof('a' * 64, 'b' * 64)
            for side in ('disabled', 'enabled'):
                pair[side]['owners'][0]['pid'] = 3100
            pair['enabled']['frames'][0]['firstObservedMonotonicNs'] = 400

            def capture(_instance, enabled, _profile):
                if enabled:
                    evidence_dir = output / 'apps/qq'
                    evidence_dir.mkdir(parents=True, exist_ok=True)
                    (evidence_dir / 'processes-enabled.txt').write_text(process_text)
                    (evidence_dir / 'logcat-enabled.txt').write_text(logs)
                return pair['enabled' if enabled else 'disabled']

            with patch.object(app.smoke, 'configure_app_id'), \
                    patch.object(app, 'checked_digest'), \
                    patch.object(app.smoke, 'adb', return_value=SimpleNamespace(stdout=b'', stderr=b'', returncode=0)), \
                    patch.object(app.smoke, 'mobile_input'), \
                    patch.object(app.smoke, 'save'), \
                    patch.object(app.smoke, 'snapshot'), \
                    patch.object(app.runtime_probe, 'manager_state', return_value={'scopeVerified': True}), \
                    patch.object(app.runtime_probe, 'installed_identity', return_value=installed), \
                    patch.object(app.runtime_policy, 'validate_request_evidence', return_value=True), \
                    patch.object(app.AppSmoke, 'capture', autospec=True, side_effect=capture):
                report = instance.qq(instance.inputs['qq'], candidate)

            evidence_dir = output / 'apps/qq'
            process_file = evidence_dir / 'processes-enabled.txt'
            process_ref = next(item for item in report['evidence']
                               if item['path'] == 'apps/qq/processes-enabled.txt')
            self.assertEqual(hashlib.sha256(process_file.read_bytes()).hexdigest(), process_ref['sha256'])
            self.assertIn('runtimeDiagnostics', report)
            self.assertEqual('READY', report['runtimeLifecycleEvidence']['qfixEvidence']['status'])
            saved = json.loads((evidence_dir / 'report.json').read_text())
            self.assertEqual(report['runtimeDiagnostics'], saved['runtimeDiagnostics'])
            self.assertEqual(report['runtimeLifecycleEvidence'], saved['runtimeLifecycleEvidence'])

    def test_memory_units_are_normalized(self):
        self.assertEqual(app.memory_mib('1.5GiB / 16GiB'), 1536)
        self.assertEqual(app.memory_mib('512MiB / 16GiB'), 512)
        with self.assertRaises(ValueError):
            app.memory_mib('')

    def test_only_the_official_single_signer_is_accepted(self):
        digest = 'a' * 64
        prepare.verify_signer('Signer #1 certificate SHA-256 digest: ' + digest, digest)
        prepare.verify_signer('V2 Signer: certificate SHA-256 digest: ' + digest, digest)
        prepare.verify_signer('V3.0 Signer: certificate SHA-256 digest: ' + digest, digest)
        with self.assertRaises(ValueError):
            prepare.verify_signer('Signer #1 certificate SHA-256 digest: ' + 'b' * 64, digest)
        with self.assertRaises(ValueError):
            prepare.verify_signer('Signer #1 certificate SHA-256 digest: ' + digest + '\nSigner #2 certificate SHA-256 digest: ' + digest, digest)
