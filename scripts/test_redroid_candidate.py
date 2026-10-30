import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.ci import redroid_app_smoke as runtime
from scripts.ci import redroid_prepare_apps as prepare
from scripts.ci.runtime_task import runtime_result
from scripts.ci.codex_adaptation import sha256
from scripts.ci.candidate_selection import recognition_identity
from scripts.ci.semantic_migration import load_migration_bundle
from scripts.test_semantic_migration import ROOT as SEMANTIC_ROOT, SemanticMigrationTest


class CandidateRuntimeTest(unittest.TestCase):
    def test_qq_split_acceptance_requires_installed_hash_captures(self):
        import hashlib
        from scripts.ci.runtime_task import _validate_qq_raw_evidence
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'capture.txt'
            path.write_bytes(b'capture')
            smoke = {'evidence': [{'path': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}
            candidate = self.candidate('qq')
            candidate['apkFiles'].append({'role': 'split', 'path': 'split.apk', 'sha256': 'b' * 64})
            with self.assertRaisesRegex(ValueError, 'split APK installed identity evidence is missing'):
                _validate_qq_raw_evidence(smoke, root, candidate, {})

    def test_installs_the_exact_base_and_splits_together_and_rejects_tampering(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = self.candidate()
            report['apkFiles'] = []
            for index, payload in enumerate((b'base', b'native', b'language')):
                path = root / f'{index}.apk'
                path.write_bytes(payload)
                report['apkFiles'].append({'role': 'base' if index == 0 else 'split',
                                          'path': path.name, 'sha256': hashlib.sha256(payload).hexdigest()})
            report['identity']['apkSha256'] = report['apkFiles'][0]['sha256']
            adb = MagicMock()
            runtime.install_candidate(adb, root, report)
            self.assertEqual(('install-multiple', '-r', *(str((root / f'{index}.apk').resolve()) for index in range(3))),
                             adb.adb.call_args.args)
            adb.reset_mock()
            (root / '1.apk').write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                runtime.install_candidate(adb, root, report)
            adb.adb.assert_not_called()

    def candidate(self, app='wechat', status='PREPARED'):
        identities = {
            'wechat': ('com.tencent.mm', '0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c'),
            'qq': ('com.tencent.mobileqq', 'ea6e97ad6c34f7039a9c6daba732c97d0e098e83ede2b4d52c76eb0184ac7a38'),
            'wecom': ('com.tencent.wework', '66896ae0aae48b9e963a3e034fa2ca813ca2f661f53619a92263a8a5c2e3f388'),
        }
        package, signer = identities[app]
        digest = 'a' * 64
        return {
            'schemaVersion': 1, 'appId': app, 'status': status,
            'identity': {'packageName': package, 'versionName': '8.0.69',
                         'versionCode': 3040, 'abi': 'arm64-v8a',
                         'apkSha256': digest, 'signerSha256': signer},
            'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/weixin8069android3040_arm64.apk',
            'apkFiles': [{'path': f'{app}.apk', 'role': 'base', 'sha256': digest}],
            'expectedPlan': {'selection': 'UNIQUE', 'ruleId': f'{app}_rule_001',
                             'strategyId': f'{app}_strategy_001', 'targets': {'target': {}}},
            'staticReport': 'static-check.json', 'staticReportSha256': 'c' * 64,
            'ruleCatalogSha256': 'd' * 64,
        }

    def test_candidate_preparation_writes_only_app_scope_not_targets_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, static, module = SemanticCandidatePreparationTest.fixture(root)
            original = copy.deepcopy(report)
            inputs = prepare.write_candidate(report, root, static_report=static, module_identity=module)
            self.assertEqual({'wechat'}, set(inputs))
            self.assertEqual('com.tencent.mm\n',
                             (root / 'resources/META-INF/xposed/scope.list').read_text())
            self.assertFalse(any(root.rglob('targets.json')))
            self.assertFalse((root / 'assets/compatibility-local').exists())
            self.assertEqual(report, original)

    def test_rejects_candidate_whose_static_input_identity_disagrees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, static, module = SemanticCandidatePreparationTest.fixture(root)
            report['apkFiles'][0]['sha256'] = 'b' * 64
            with self.assertRaisesRegex(ValueError, 'Candidate APK hashes'):
                prepare.write_candidate(report, root, static_report=static, module_identity=module)

    def test_setup_failure_preserves_standard_candidate_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, static, module_identity = SemanticCandidatePreparationTest.fixture(root)
            inputs = prepare.write_candidate(candidate, root, static_report=static,
                                             module_identity=module_identity)
            (root / 'inputs.json').write_text(json.dumps(inputs))
            (root / 'candidate-report.json').write_text(json.dumps(candidate))
            (root / 'module').mkdir()
            (root / 'module/app-debug.apk').write_bytes(b'module')
            app = runtime.AppSmoke(root, MagicMock(output=root))
            with patch.object(app, 'setup', side_effect=RuntimeError('Framework unavailable')), \
                 patch.object(app, 'monitor'):
                with self.assertRaisesRegex(RuntimeError, 'Framework unavailable'):
                    app.execute()
            report = json.loads((root / 'evidence/smoke-report.json').read_text())
            self.assertEqual(report['status'], 'ACCEPTANCE_UNAVAILABLE')
            self.assertEqual(report['apkSha256'], candidate['identity']['apkSha256'])
            self.assertIn('Framework unavailable', report['reason'])

    def test_wechat_ui_report_matches_shared_runtime_evidence_contract(self):
        report = self.candidate('wechat', 'STATIC_VERIFIED_PENDING_RUNTIME')
        with tempfile.TemporaryDirectory() as directory:
            evidence_root = Path(directory)
            evidence_directory = evidence_root / 'apps/wechat'
            evidence_directory.mkdir(parents=True)
            (evidence_directory / 'logcat.txt').write_text('captured app log')
            module_identity = {'sha256': 'b' * 64, 'sourceTreeSha256': 'c' * 64,
                               'ruleCatalogSha256': 'd' * 64, 'recognitionSha256': 'e' * 64,
                               'dependencyLockSha256': 'f' * 64}
            result = runtime.pipeline_report({
                'appId': 'wechat', 'status': 'RUNTIME_SMOKE_VERIFIED',
                'baseline': {'moduleEnabled': False, 'loginPageReady': True, 'tabletEntry': False},
                'qrPage': 'LoginAsExDeviceUI', 'moduleApkSha256': 'b' * 64,
                'evidenceDirectory': str(evidence_directory),
            }, report, module_identity, evidence_root)
        self.assertEqual('RUNTIME_VERIFIED', runtime_result(report, result, 'success')['status'])
        self.assertEqual(result['baselineEvidence']['moduleEnabled'], False)

    def test_only_the_official_single_signer_is_accepted(self):
        digest = 'a' * 64
        prepare.verify_signer('Signer #1 certificate SHA-256 digest: ' + digest, digest)
        prepare.verify_signer('V2 Signer: certificate SHA-256 digest: ' + digest, digest)
        prepare.verify_signer('V3.0 Signer: certificate SHA-256 digest: ' + digest, digest)
        with self.assertRaises(ValueError):
            prepare.verify_signer('Signer #1 certificate SHA-256 digest: ' + 'b' * 64, digest)
        with self.assertRaises(ValueError):
            prepare.verify_signer('Signer #1 certificate SHA-256 digest: ' + digest +
                                  '\nSigner #2 certificate SHA-256 digest: ' + digest, digest)


class SemanticCandidatePreparationTest(unittest.TestCase):
    @staticmethod
    def fixture(root):
        profile = next(row for row in load_migration_bundle(SEMANTIC_ROOT).profiles
                       if row['appId'] == 'wechat')
        static = SemanticMigrationTest('runTest').report(profile)
        apk = root / 'wechat.apk'
        apk.write_bytes(b'independent schema2 candidate input')
        digest = sha256(apk)
        static['identity']['apkSha256'] = digest
        static['inputHashes'] = [{'path': apk.name, 'sha256': digest}]
        static['snapshot']['archives'][0]['containerSha256'] = digest
        static_path = root / 'static-check.json'
        static_path.write_text(json.dumps(static, indent=2) + '\n')
        recognition, recognition_sha = recognition_identity(static)
        execution = static['executionIdentity']
        module = {
            'schemaVersion': 2, 'sha256': 'b' * 64,
            'ruleCatalogSha256': static['ruleCatalog']['ruleCatalogSha256'],
            'recognition': recognition, 'recognitionSha256': recognition_sha,
            'dexkit': execution,
            'sourceTreeSha256': execution['sourceTreeSha256'],
            'dependencyLockSha256': execution['dependencyLockSha256'],
            'routeConfigurationSha256': execution['routeConfigurationSha256'],
            'fingerprintFileSha256': execution['fingerprintFileSha256'],
        }
        candidate = {
            'schemaVersion': 2, 'appId': 'wechat', 'status': 'PREPARED',
            'identity': static['identity'], 'apkFiles': [{'path': apk.name, 'role': 'base', 'sha256': digest}],
            'ruleCatalog': static['ruleCatalog'],
            'ruleCatalogSha256': static['ruleCatalog']['ruleCatalogSha256'],
            'snapshot': static['snapshot'], 'expectedPlan': static['selection']['plan'],
            'expectedExecutionSha256': static['selection']['executionSha256'],
            'recognition': recognition, 'recognitionSha256': recognition_sha,
            'staticReport': static_path.name, 'staticReportSha256': sha256(static_path),
            'module': module,
        }
        return candidate, static, module

    def test_write_candidate_accepts_schema2_complete_plan_and_matching_input_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate, static, module = self.fixture(root)
            result = prepare.write_candidate(candidate, root, static_report=static,
                                             module_identity=module)
            self.assertEqual({'wechat': candidate}, result)
            self.assertEqual('com.tencent.mm\n',
                             (root / 'resources/META-INF/xposed/scope.list').read_text())

    def test_write_candidate_rejects_schema_and_semantic_mismatches_without_writes(self):
        mutations = (
            lambda c, s, m: c.update(schemaVersion=1),
            lambda c, s, m: c.update(expectedPlan={}),
            lambda c, s, m: c.update(recognitionSha256='0' * 64),
            lambda c, s, m: s['selection'].update(status='CONFLICT'),
            lambda c, s, m: s['inputHashes'][0].update(sha256='0' * 64),
            lambda c, s, m: m.update(recognitionSha256='0' * 64),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                candidate, static, module = self.fixture(root)
                mutate(candidate, static, module)
                scope = root / 'resources/META-INF/xposed/scope.list'
                scope.parent.mkdir(parents=True)
                scope.write_bytes(b'preserve existing scope')
                candidate_file = root / 'candidate-report.json'
                candidate_file.write_bytes(b'preserve candidate')
                metadata = root / 'module-identity.json'
                metadata.write_bytes(b'preserve metadata')
                with self.assertRaises(ValueError):
                    prepare.write_candidate(candidate, root, static_report=static,
                                            module_identity=module)
                self.assertEqual(b'preserve existing scope', scope.read_bytes())
                self.assertEqual(b'preserve candidate', candidate_file.read_bytes())
                self.assertEqual(b'preserve metadata', metadata.read_bytes())

    def test_main_rejects_legacy_schema_before_overwriting_candidate_or_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_path = root / 'candidate-report.json'
            candidate_path.write_bytes(b'{"schemaVersion":1}')
            metadata = root / 'module-identity.json'
            metadata.write_bytes(b'preserve metadata')
            candidate_before, metadata_before = candidate_path.read_bytes(), metadata.read_bytes()
            with patch('sys.argv', ['redroid_prepare_apps', '--root', str(root),
                                    '--candidate', str(candidate_path)]), \
                 patch.object(prepare, 'prepare_module') as prepare_module:
                with self.assertRaisesRegex(ValueError, 'schema version 2'):
                    prepare.main()
            prepare_module.assert_not_called()
            self.assertEqual(candidate_before, candidate_path.read_bytes())
            self.assertEqual(metadata_before, metadata.read_bytes())

    def test_main_rejects_schema2_mismatch_before_overwriting_candidate_or_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate, _, _ = self.fixture(root)
            candidate['expectedPlan'] = {}
            candidate_path = root / 'candidate-report.json'
            candidate_path.write_text(json.dumps(candidate))
            metadata = root / 'module-identity.json'
            metadata.write_bytes(b'preserve metadata')
            candidate_before, metadata_before = candidate_path.read_bytes(), metadata.read_bytes()
            with patch('sys.argv', ['redroid_prepare_apps', '--root', str(root),
                                    '--candidate', str(candidate_path)]), \
                 patch.object(prepare, 'prepare_module') as prepare_module:
                with self.assertRaisesRegex(ValueError, 'complete semantic plan'):
                    prepare.main()
            prepare_module.assert_not_called()
            self.assertEqual(candidate_before, candidate_path.read_bytes())
            self.assertEqual(metadata_before, metadata.read_bytes())


if __name__ == '__main__':
    unittest.main()
