import json
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from scripts.ci import redroid_app_smoke as app
from scripts.ci import redroid_prepare_apps as prepare
from scripts.ci import runtime_task


class ReDroidApplicationSmokeTest(unittest.TestCase):
    def test_wecom_summary_only_fixture_is_rejected_without_production_raw_captures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_root = root / 'candidate'
            candidate_root.mkdir()
            output = root / 'runtime'
            module_apk = root / 'module.apk'
            module_bytes = b'wecom-test-module'
            module_apk.write_bytes(module_bytes)
            apk_sha = hashlib.sha256(b'official-wecom-apk').hexdigest()
            module_sha = hashlib.sha256(module_bytes).hexdigest()
            source_tree_sha = 'c' * 64
            catalog_sha = 'd' * 64
            route_configuration_sha = '7' * 64
            dependency_lock_sha = 'f' * 64
            fingerprint_file_sha = '8' * 64
            route_candidates = ['wecom_local_001']
            route_projection = {'appId': 'wecom', 'candidates': route_candidates}
            route_sha = hashlib.sha256(json.dumps(
                route_projection, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            fingerprint = {'schemaVersion': 1, 'sourceTreeSha256': source_tree_sha,
                           'routeConfigurationSha256': route_configuration_sha,
                           'dependencyLockSha256': dependency_lock_sha}
            module = {'sha256': module_sha, 'sourceTreeSha256': source_tree_sha,
                      'sourceCommit': 'test-source-commit',
                      'ruleCatalogSha256': catalog_sha, 'routeSha256': route_sha,
                      'routeConfigurationSha256': route_configuration_sha,
                      'fingerprintFileSha256': fingerprint_file_sha,
                      'dependencyLockSha256': dependency_lock_sha, 'fingerprint': fingerprint,
                      'route': route_projection, 'dexkit': {'dexkitAndroidApiSha256': '1' * 64,
                                                            'dexkitAndroidArm64NativeSha256': '2' * 64}}
            identity = {'packageName': 'com.tencent.wework', 'versionName': '5.0.8',
                        'versionCode': 70675, 'abi': 'arm64-v8a', 'apkSha256': apk_sha,
                        'signerSha256': '66896ae0aae48b9e963a3e034fa2ca813ca2f661f53619a92263a8a5c2e3f388'}
            targets = {
                'device_type': {'descriptor': 'Lcom/tencent/wework/foundation/logic/Application;->a()I',
                                'shape': {'parameters': [], 'returnType': 'int', 'isStatic': False}},
                'app_pad': {'descriptor': 'Lcom/tencent/wework/foundation/logic/Application;->b()Z',
                            'shape': {'parameters': [], 'returnType': 'boolean', 'isStatic': True}},
                'service_pad': {'descriptor': 'Lcom/tencent/wework/foundation/impl/WeworkServiceImpl;->c()Z',
                                'shape': {'parameters': [], 'returnType': 'boolean', 'isStatic': False}},
            }
            plan = {'selection': 'UNIQUE', 'ruleId': 'wecom_local_001',
                    'strategyId': 'wecom_local_tablet_001', 'targets': targets}
            catalog = {'schemaVersion': 1, 'appId': 'wecom', 'ruleCatalogSha256': catalog_sha,
                       'definitions': [{'ruleId': 'wecom_local_001',
                                        'strategyId': 'wecom_local_tablet_001',
                                        'requiredTargetIds': ['device_type', 'app_pad', 'service_pad']}],
                       'bindings': [{'ruleId': 'wecom_local_001', 'min': 70675, 'max': None}],
                       'strategyIds': ['wecom_local_tablet_001']}
            candidate_apk = candidate_root / 'apk-inputs/base.apk'
            candidate_apk.parent.mkdir(parents=True)
            candidate_apk.write_bytes(b'official-wecom-apk')
            candidate = {'schemaVersion': 1, 'appId': 'wecom', 'status': 'PREPARED',
                         'identity': identity,
                         'sourceUrl': 'https://dldir1.qq.com/wework/work_weixin/WeCom_android_5.0.8.70675_arm64_100001.apk',
                         'apkFiles': [{'path': 'apk-inputs/base.apk', 'role': 'base', 'sha256': apk_sha}],
                         'expectedPlan': plan,
                         'route': dict(route_projection, samplingMode='DIRECT'), 'routeSha256': route_sha,
                         'ruleCatalogSha256': catalog_sha, 'module': module,
                         'staticReport': 'static-check.json'}
            static = {'appId': 'wecom', 'analysisScope': 'STATIC_ONLY', 'identity': identity,
                      'inputHashes': [{'path': 'apk-inputs/base.apk', 'sha256': apk_sha}],
                      'ruleCatalogSha256': catalog_sha, 'ruleCatalog': catalog,
                      'executionIdentity': {'ruleCatalogSha256': catalog_sha,
                                            'fingerprintFileSha256': fingerprint_file_sha,
                                            'sourceTreeSha256': source_tree_sha,
                                            'routeConfigurationSha256': route_configuration_sha,
                                            'dependencyLockSha256': dependency_lock_sha},
                      'routeCandidates': route_candidates,
                      'selection': {'status': 'SELECTED', 'selectedRuleId': 'wecom_local_001',
                                    'strategyId': 'wecom_local_tablet_001', 'targets': targets}}
            static_path = candidate_root / 'static-check.json'
            static_path.write_text(json.dumps(static))
            candidate['staticReportSha256'] = hashlib.sha256(static_path.read_bytes()).hexdigest()
            candidate_path = candidate_root / 'candidate.json'
            candidate_path.write_text(json.dumps(candidate))
            acceptance_path = root / 'compatibility/wecom/acceptance.json'
            acceptance_path.parent.mkdir(parents=True)
            acceptance_path.write_text(json.dumps({'schemaVersion': 1, 'appId': 'wecom', 'records': []}))
            regression_path = candidate_root / 'regression/results.json'
            regression_path.parent.mkdir(parents=True)
            regression_path.write_text(json.dumps([{
                'appId': 'wecom', 'status': 'NO_HISTORICAL_SAMPLES',
                'acceptanceSnapshotSha256': hashlib.sha256(acceptance_path.read_bytes()).hexdigest(),
                'initialMigration': True,
            }]))

            def run_ui_smoke(app_id, apps_root, inputs, tested_module_hash):
                evidence_dir = apps_root / app_id
                evidence_dir.mkdir(parents=True)
                (evidence_dir / 'logcat.txt').write_text('captured WeCom logcat')
                (evidence_dir / 'screen.xml').write_text('<hierarchy/>')
                return {'appId': app_id, 'status': 'RUNTIME_SMOKE_VERIFIED',
                        'evidenceDirectory': str(evidence_dir), 'moduleApkSha256': tested_module_hash,
                        'baseline': 'NO_TABLET_ENTRY', 'qrPage': 'LoginQrCodeForAndroidPad'}

            instance = object.__new__(app.AppSmoke)
            instance.probe = SimpleNamespace(output=output)
            instance.inputs = {'wecom': {}}
            instance.module_hash = module_sha
            instance.module_identity = module
            instance.restart = lambda: None
            with patch('scripts.ci.redroid_ui_smoke.probe', side_effect=run_ui_smoke):
                produced = instance.run_candidate({'appId': 'wecom', 'identity': identity})

            self.assertEqual('RUNTIME_SMOKE_VERIFIED', produced['status'])
            with patch('scripts.ci.candidate_selection.prepare_module', return_value=module):
                with self.assertRaisesRegex(ValueError, 'missing required UI, process, module or manager snapshots'):
                    runtime_task.verify_validation_files(
                        candidate_path, module_apk, regression_path,
                        output / 'evidence/smoke-report.json', output, 'success', root,
                        acceptance_path, initial_migration=True,
                    )
            self.assertEqual(module_sha, produced['moduleApkSha256'])
            self.assertEqual(source_tree_sha, produced['moduleSourceTreeSha256'])
            self.assertEqual(catalog_sha, produced['ruleCatalogSha256'])
            self.assertEqual(route_sha, produced['routeSha256'])
            self.assertEqual(dependency_lock_sha, produced['dependencyLockSha256'])
            self.assertEqual({'apps/wecom/logcat.txt', 'apps/wecom/screen.xml'},
                             {item['path'] for item in produced['evidence']})

            mismatched_module = app.pipeline_report(
                {'appId': 'wecom', 'status': 'RUNTIME_SMOKE_VERIFIED',
                 'evidenceDirectory': str(output / 'apps/wecom'), 'moduleApkSha256': 'e' * 64},
                {'appId': 'wecom', 'identity': identity}, module, output,
            )
            self.assertEqual('RUNTIME_REJECTED', mismatched_module['status'])

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

    def test_qq_compatibility_and_hook_installation_must_share_a_captured_owner(self):
        logs = '\n'.join([
            'x x 123 x I ImPad: status=COMPATIBLE',
            'x x 456 x I ImPad: installed 1 QQ hooks',
        ])
        self.assertFalse(app.qq_compatible_hook_installed(logs, {123, 456}))
        logs += '\nx x 123 x I ImPad: installed 1 QQ hooks'
        self.assertTrue(app.qq_compatible_hook_installed(logs, {123}))

    def test_production_qq_smoke_passes_logcat_text_to_compatibility_gate(self):
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
                    patch.object(app.AppSmoke, 'capture', autospec=True, side_effect=capture):
                report = instance.run_candidate({
                    'appId': 'qq',
                    'identity': {'packageName': 'com.tencent.mobileqq', 'abi': 'arm64-v8a',
                                 'apkSha256': 'a' * 64},
                })
                written_report = json.loads((output / 'evidence/smoke-report.json').read_text())

        self.assertEqual('ACCEPTANCE_UNAVAILABLE', report['status'])
        self.assertEqual('RuntimeError: Native, QFix, or hook-before-request evidence is missing',
                         report['reason'])
        self.assertEqual('REQUEST_APP_ID_CONTRAST', report['acceptanceKind'])
        self.assertEqual(capture_proof, report['requestIdentityEvidence'])
        self.assertEqual(report, written_report)

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
