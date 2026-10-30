import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.ci.runtime_task import main, runtime_result, verify_cli, register_cli


class RuntimeTaskTest(unittest.TestCase):
    def setUp(self):
        self.identity = {'packageName': 'com.tencent.mm', 'versionName': '8.0.69',
                         'versionCode': 3040, 'abi': 'arm64-v8a',
                         'apkSha256': 'a' * 64,
                         'signerSha256': '0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c'}
        self.report = {'schemaVersion': 1, 'appId': 'wechat',
                       'status': 'STATIC_VERIFIED_PENDING_RUNTIME', 'identity': self.identity,
                       'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/weixin8069android3040_arm64.apk',
                       'expectedPlan': {'selection': 'UNIQUE', 'ruleId': 'wechat_tablet_login_001',
                                        'strategyId': 'wechat_tablet_login_behavior_001',
                                        'targets': {'tablet': {}, 'login': {}}}}
        self.smoke = {'appId': 'wechat', 'packageName': self.identity['packageName'],
                      'abi': self.identity['abi'], 'status': 'RUNTIME_SMOKE_VERIFIED',
                      'baseline': 'NO_TABLET_ENTRY', 'hookIds': ['tablet', 'login'],
                      'qrPage': 'LoginAsExDeviceUI', 'apkSha256': self.identity['apkSha256']}

    def test_matching_exact_runtime_evidence_changes_only_runtime_status(self):
        original = copy.deepcopy(self.report)
        result = runtime_result(self.report, self.smoke, 'success')
        self.assertEqual('RUNTIME_VERIFIED', result['status'])
        self.assertEqual([], result['blockers'])
        self.assertEqual(self.report, original)
        self.assertNotIn('suggestedProfile', result)

    def test_mismatched_app_identity_behavior_or_apk_is_rejected(self):
        cases = [
            ({'appId': 'qq'}, 'appId'),
            ({'packageName': 'org.example.other'}, 'packageName'),
            ({'abi': 'x86_64'}, 'abi'),
            ({'hookIds': ['tablet']}, 'hookIds'),
            ({'qrPage': 'SplashActivity'}, 'qrPage'),
            ({'apkSha256': 'b' * 64}, 'apkSha256'),
        ]
        for fields, changed in cases:
            with self.subTest(changed=changed):
                result = runtime_result(self.report, dict(self.smoke, **fields), 'success')
                self.assertEqual('RUNTIME_REJECTED', result['status'])

    def test_cancelled_or_skipped_runtime_remains_pending(self):
        for conclusion in ('cancelled', 'skipped'):
            result = runtime_result(self.report, self.smoke, conclusion)
            self.assertEqual('STATIC_VERIFIED_PENDING_RUNTIME', result['status'])
            self.assertEqual(conclusion, result['runtimeConclusion'])
            self.assertTrue(result['blockers'])

    def test_non_static_candidate_cannot_enter_runtime_acceptance(self):
        report = copy.deepcopy(self.report)
        report['status'] = 'INCOMPATIBLE'
        with self.assertRaisesRegex(ValueError, 'static verification'):
            runtime_result(report, self.smoke, 'success')

    def test_explicit_app_id_cannot_override_candidate_application(self):
        with self.assertRaises(ValueError):
            runtime_result(self.report, self.smoke, 'success', 'qq')

    def test_runtime_result_command_writes_report_without_acceptance_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'candidate-report.json').write_text(json.dumps(self.report))
            smoke_path = root / 'smoke.json'
            smoke_path.write_text(json.dumps(self.smoke))
            with patch('sys.argv', ['runtime_task', '--app-id', 'wechat', '--directory', str(root),
                                    '--smoke', str(smoke_path), '--conclusion', 'success']), \
                 patch('scripts.ci.runtime_task.subprocess') as subprocess:
                main()
            subprocess.assert_not_called()
            result = json.loads((root / 'runtime-result.json').read_text())
            self.assertEqual('RUNTIME_VERIFIED', result['status'])
            self.assertFalse((root / 'acceptance.json').exists())
            self.assertFalse((root / 'targets.json').exists())

    def test_verify_and_register_clis_do_not_expose_initial_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = ['--app-id', 'wechat', '--candidate', str(root / 'candidate.json'),
                      '--module-apk', str(root / 'module.apk'), '--regression', str(root / 'regression.json'),
                      '--smoke', str(root / 'smoke.json'), '--evidence-root', str(root),
                      '--acceptance', str(root / 'acceptance.json'), '--repository', str(root),
                      '--conclusion', 'success']
            with patch('scripts.ci.runtime_task.verify_validation_files') as verifier, \
                 self.assertRaises(SystemExit):
                verify_cli(common + ['--initial-migration', '--output', str(root / 'verify.json')])
            verifier.assert_not_called()
            register_args = common + ['--expected-head', 'a' * 40, '--initial-migration',
                                      '--output', str(root / 'register.json')]
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'owner', 'GITHUB_REPOSITORY_OWNER': 'owner'}), \
                 patch('scripts.ci.runtime_task.verify_validation_files') as verifier, \
                 self.assertRaises(SystemExit):
                register_cli(register_args)
            verifier.assert_not_called()
            self.assertFalse((root / 'acceptance.json').exists())
            self.assertFalse((root / 'register.json').exists())

    def test_verify_cli_emits_schema_two_without_internal_migration_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = {'schemaVersion': 2, 'outcome': 'VERIFIED', 'failedStage': None,
                      'candidate': {'appId': 'wechat'}, '_migrationBundleObject': object()}
            args = ['--app-id', 'wechat', '--candidate', str(root / 'candidate.json'),
                    '--module-apk', str(root / 'module.apk'), '--regression', str(root / 'regression.json'),
                    '--smoke', str(root / 'smoke.json'), '--evidence-root', str(root),
                    '--acceptance', str(root / 'compatibility/wechat/acceptance.json'),
                    '--repository', str(root), '--conclusion', 'success', '--output', str(root / 'verify.json')]
            with patch('scripts.ci.runtime_task.verify_validation_files', return_value=report):
                verify_cli(args)
            output = json.loads((root / 'verify.json').read_text())
            self.assertEqual(2, output['schemaVersion'])
            self.assertNotIn('_migrationBundleObject', output)

    def test_semantic_runtime_gate_includes_same_apk_v2_case_and_keeps_migration_ids_separate(self):
        import shutil
        from scripts.ci.runtime_task import _semantic_stage_context
        from scripts.ci.semantic_migration import load_migration_bundle
        from scripts.ci.semantic_regression_baseline import load_semantic_regression_baseline
        from scripts.test_semantic_migration import SemanticMigrationTest, ROOT
        from scripts.test_semantic_regression_baseline import SemanticRegressionBaselineTest
        baseline_fixture = SemanticRegressionBaselineTest('runTest')
        baseline_fixture.setUp()
        self.addCleanup(baseline_fixture.doCleanups)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            shutil.copytree(ROOT / 'compatibility', root / 'compatibility')
            bundle = load_migration_bundle(root)
            profile = next(row for row in bundle.profiles if row['appId'] == 'wechat')
            record = baseline_fixture.matching_migration_record(profile, 'same-apk-v2-record')
            table_path = root / 'compatibility/wechat/acceptance-v2.json'
            table_path.write_text(json.dumps({'schemaVersion': 2, 'appId': 'wechat',
                                              'records': [record]}, indent=2) + '\n')
            baseline = load_semantic_regression_baseline(root, 'wechat', bundle)
            self.assertEqual('MIGRATION', baseline.cases[0]['kind'])
            self.assertEqual('ACCEPTED_V2', baseline.cases[-1]['kind'])
            self.assertEqual([item['sha256'] for item in baseline.cases[0]['apkFiles']],
                             [item['sha256'] for item in baseline.cases[-1]['apkFiles']])
            candidate = {'schemaVersion': 2, 'appId': 'wechat', 'identity': record['identity'],
                'apkFiles': record['apkFiles'], 'expectedPlan': record['completePlan'],
                'expectedExecutionSha256': record['executionSha256'],
                'ruleCatalog': record['staticReport']['ruleCatalog'],
                'ruleCatalogSha256': record['staticReport']['ruleCatalog']['ruleCatalogSha256'],
                'snapshot': record['staticReport']['snapshot'],
                'recognition': record['recognition'],
                'recognitionSha256': record['recognitionSha256']}
            reports_root = root / 'regression'
            reports_root.mkdir()
            rows, reports, report_paths = [], {}, {}
            for case in baseline.cases:
                if case['kind'] == 'MIGRATION':
                    source_profile = next(row for row in bundle.profiles
                                          if row['profileId'] == case['profileId'])
                    fixed = SemanticMigrationTest.report(SemanticMigrationTest('runTest'), source_profile)
                else:
                    fixed = case['savedStaticReport']
                fixed_path = reports_root / f"{case['recordId']}.json"
                raw = json.dumps(fixed).encode()
                fixed_path.write_bytes(raw)
                source_case = {'kind': case['kind'], 'recordId': case['recordId'],
                               'profileId': case['profileId'],
                               'sourceSha256': case['acceptanceSnapshotSha256']}
                rows.append({'appId': case['appId'], 'recordId': case['recordId'],
                    'identity': case['identity'], 'profileId': case['profileId'],
                    'initialMigration': False,
                    'acceptanceSnapshotSha256': case['acceptanceSnapshotSha256'],
                    'migrationHistorySha256': bundle.history_sha256,
                    'migrationExpectationsSha256': bundle.expectations_sha256,
                    'baselineSha256': baseline.sha256, 'v2SnapshotSha256': baseline.v2_sha256,
                    'baselineSourceCase': source_case,
                    'staticEvidence': {'path': fixed_path.name,
                                       'sha256': hashlib.sha256(raw).hexdigest()},
                    'status': 'PASSED'})
                reports[case['recordId']] = fixed
                report_paths[case['recordId']] = fixed_path
            regression_path = reports_root / 'results.json'
            regression_path.write_text(json.dumps(rows))
            regression = {'records': rows, 'evidence': {
                'path': str(regression_path),
                'sha256': hashlib.sha256(regression_path.read_bytes()).hexdigest()}}
            gate_bundle, gate, same_baseline = _semantic_stage_context(
                candidate, record['module'], record['staticReport'], regression,
                root, migration_bundle=bundle, regression_baseline=baseline)
            self.assertIs(bundle, gate_bundle)
            self.assertIs(baseline, same_baseline)
            self.assertEqual([case['recordId'] for case in baseline.cases],
                             gate['regressionRecordIds'])
            self.assertEqual([case['recordId'] for case in baseline.cases
                              if case['kind'] == 'MIGRATION'], gate['recordIds'])
            self.assertIn('same-apk-v2-record', gate['regressionRecordIds'])

            v2_report_path = report_paths['same-apk-v2-record']
            v2_report_raw = v2_report_path.read_bytes()
            v2_report_path.write_bytes(v2_report_raw + b' ')
            with self.assertRaisesRegex(ValueError, 'static report changed'):
                _semantic_stage_context(candidate, record['module'], record['staticReport'],
                    regression, root, migration_bundle=bundle, regression_baseline=baseline)
            v2_report_path.write_bytes(v2_report_raw)

            v2_row = next(row for row in rows if row['recordId'] == 'same-apk-v2-record')
            rows.remove(v2_row)
            regression_path.write_text(json.dumps(rows))
            regression['evidence']['sha256'] = hashlib.sha256(regression_path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, 'exactly one row per baseline case'):
                _semantic_stage_context(candidate, record['module'], record['staticReport'],
                    regression, root, migration_bundle=bundle, regression_baseline=baseline)

            changed = json.loads(table_path.read_text())
            changed['records'][0]['acceptedAt'] += ' '
            table_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, 'baseline changed'):
                baseline.assert_unchanged()

    def test_register_writes_only_v2_table_and_returns_its_real_path(self):
        from scripts.test_semantic_acceptance import SemanticAcceptanceTest

        fixture = SemanticAcceptanceTest('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / 'tested'
            root.mkdir()
            shutil.copytree(fixture.root / 'compatibility', root / 'compatibility')
            from scripts.ci.semantic_regression_baseline import load_semantic_regression_baseline
            verified_baseline = load_semantic_regression_baseline(root, 'wechat')
            legacy = root / 'compatibility/wechat/acceptance.json'
            legacy_bytes = legacy.read_bytes()
            candidate = {'appId': 'wechat', 'identity': fixture.record['identity'],
                         'apkFiles': fixture.record['apkFiles'],
                         'expectedPlan': fixture.record['completePlan'],
                         'expectedExecutionSha256': fixture.record['executionSha256'],
                         'recognition': fixture.record['recognition'],
                         'recognitionSha256': fixture.record['recognitionSha256'],
                         'sourceUrl': fixture.record['source']['url']}
            report = {'schemaVersion': 2, 'outcome': 'VERIFIED', 'failedStage': None,
                      'candidate': candidate, 'module': fixture.record['module'],
                      'static': fixture.static,
                      'stageEvidence': {key: fixture.record['rawStageEvidence'][key]
                                        for key in ('static', 'regression', 'runtime')},
                      'runtime': {'evidence': fixture.record['rawStageEvidence']['files']},
                      'migrationBundle': fixture.record['migrationBundle'],
                      'regressionBaseline': __import__(
                          'scripts.ci.runtime_task', fromlist=['_regression_baseline_binding']
                      )._regression_baseline_binding(verified_baseline),
                      'regressionRecordIds': [case['recordId'] for case in verified_baseline.cases],
                      'baselineSha256': verified_baseline.sha256,
                      'v2SnapshotSha256': verified_baseline.v2_sha256}
            external = root.parent / (root.name + '-runtime-result')
            external.mkdir()
            candidate_root = root / 'candidate-artifact'
            candidate_root.mkdir()
            static_path = candidate_root / 'static.json'
            static_path.write_text(json.dumps(fixture.static))
            candidate_report_path = candidate_root / 'candidate-report.json'
            regression_path = root / 'regression.json'
            regression_path.write_text('[{"status":"PASSED"}]\n')
            smoke_path = external / 'smoke.json'
            raw_path = external / 'raw.jsonl'
            smoke_path.write_text('{"status":"RUNTIME_SMOKE_VERIFIED"}\n')
            raw_path.write_text('{"event":"verified"}\n')
            static_sha = hashlib.sha256(static_path.read_bytes()).hexdigest()
            regression_sha = hashlib.sha256(regression_path.read_bytes()).hexdigest()
            smoke_sha = hashlib.sha256(smoke_path.read_bytes()).hexdigest()
            raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            candidate['staticReport'] = 'static.json'
            candidate_report_path.write_text(json.dumps(candidate))
            report['stageEvidence'] = {
                'static': {'path': 'static.json', 'sha256': static_sha},
                'regression': {'path': str(regression_path), 'sha256': regression_sha},
                'runtime': {'path': str(smoke_path), 'sha256': smoke_sha},
            }
            report['runtime'] = {'evidenceReport': {'path': 'smoke.json', 'sha256': smoke_sha},
                                 'evidence': [{'path': 'raw.jsonl', 'sha256': raw_sha}]}
            report['inputPaths'] = {'repository': str(root),
                'candidate': str(candidate_report_path),
                'regression': str(regression_path), 'smoke': str(smoke_path),
                'evidenceRoot': str(external)}
            args = ['--app-id', 'wechat', '--candidate', str(root / 'candidate.json'),
                    '--module-apk', str(root / 'module.apk'), '--regression', str(root / 'regression.json'),
                    '--smoke', str(root / 'smoke.json'), '--evidence-root', str(root),
                    '--acceptance', str(legacy), '--repository', str(root), '--conclusion', 'success',
                    '--expected-head', 'a' * 40, '--output', str(root / 'register.json')]
            head_failure_output = root / 'head-changed.json'
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'owner', 'GITHUB_REPOSITORY_OWNER': 'owner'}), \
                 patch('scripts.ci.runtime_task.subprocess.check_output',
                       side_effect=['a' * 40, 'a' * 40, 'b' * 40]), \
                 patch('scripts.ci.runtime_task.verify_validation_files', return_value=report):
                with self.assertRaisesRegex(ValueError, 'head changed'):
                    register_cli(args[:-1] + [str(head_failure_output)])
            self.assertFalse((root / 'compatibility/wechat/acceptance-v2.json').exists())
            self.assertFalse(head_failure_output.exists())
            evidence_parent = root / 'compatibility/wechat/acceptance-evidence'
            self.assertTrue(not evidence_parent.exists() or not list(evidence_parent.iterdir()))
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'owner', 'GITHUB_REPOSITORY_OWNER': 'owner'}), \
                 patch('scripts.ci.runtime_task.subprocess.check_output', return_value='a' * 40), \
                 patch('scripts.ci.runtime_task.verify_validation_files', return_value=report) as verifier:
                register_cli(args)
            self.assertEqual(2, verifier.call_count)
            result = json.loads((root / 'register.json').read_text())
            expected_path = 'compatibility/wechat/acceptance-v2.json'
            self.assertEqual(2, result['schemaVersion'])
            self.assertEqual(expected_path, result['acceptancePath'])
            table = json.loads((root / expected_path).read_text())
            self.assertEqual(2, table['schemaVersion'])
            self.assertEqual(1, len(table['records']))
            self.assertEqual(legacy_bytes, legacy.read_bytes())
            archived = table['records'][0]['evidenceArchive']['files']
            self.assertEqual(['static-report.json', 'regression-report.json',
                              'runtime-report.json', 'raw-0000-raw.jsonl'],
                             [Path(item['path']).name for item in archived])
            for item in archived:
                stored = root / item['path']
                self.assertTrue(stored.is_file())
                self.assertEqual(item['sha256'], hashlib.sha256(stored.read_bytes()).hexdigest())
            self.assertEqual([item['path'] for item in archived] +
                             [table['records'][0]['evidenceArchive']['manifestPath']],
                             result['evidencePaths'])
            manifest_reference = result['evidenceManifest']
            self.assertEqual(table['records'][0]['evidenceArchive']['manifestPath'],
                             manifest_reference['path'])
            self.assertEqual(manifest_reference['sha256'], hashlib.sha256(
                (root / manifest_reference['path']).read_bytes()).hexdigest())
            v2_path = root / expected_path
            v2_before = v2_path.read_bytes()
            rejected = dict(report, outcome='CANDIDATE_REJECTED')
            rejected_args = args[:-1] + [str(root / 'rejected.json')]
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'owner', 'GITHUB_REPOSITORY_OWNER': 'owner'}), \
                 patch('scripts.ci.runtime_task.subprocess.check_output', return_value='a' * 40), \
                 patch('scripts.ci.runtime_task.verify_validation_files', return_value=rejected):
                with self.assertRaisesRegex(ValueError, 'not VERIFIED'):
                    register_cli(rejected_args)
            self.assertEqual(v2_before, v2_path.read_bytes())
            self.assertFalse((root / 'rejected.json').exists())

            from scripts.ci.semantic_regression_baseline import load_semantic_regression_baseline
            current_baseline = load_semantic_regression_baseline(root, 'wechat')
            report['regressionBaseline'] = __import__(
                'scripts.ci.runtime_task', fromlist=['_regression_baseline_binding']
            )._regression_baseline_binding(current_baseline)
            archive_root = root / 'compatibility/wechat/acceptance-evidence'
            archives_before = sorted(path.name for path in archive_root.iterdir())
            existing_archive = archive_root / result['recordId']
            archive_before = {path.relative_to(existing_archive).as_posix(): path.read_bytes()
                              for path in existing_archive.rglob('*') if path.is_file()}
            collision_output = root / 'archive-collision.json'
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'owner', 'GITHUB_REPOSITORY_OWNER': 'owner'}), \
                 patch('scripts.ci.runtime_task.uuid.uuid4', return_value=__import__('uuid').UUID(result['recordId'])), \
                 patch('scripts.ci.runtime_task.subprocess.check_output', return_value='a' * 40), \
                 patch('scripts.ci.runtime_task.verify_validation_files', return_value=report):
                with self.assertRaises(FileExistsError):
                    register_cli(args[:-1] + [str(collision_output)])
            self.assertEqual(v2_before, v2_path.read_bytes())
            self.assertEqual(archives_before, sorted(path.name for path in archive_root.iterdir()))
            self.assertEqual(archive_before,
                {path.relative_to(existing_archive).as_posix(): path.read_bytes()
                 for path in existing_archive.rglob('*') if path.is_file()})
            self.assertFalse(collision_output.exists())

            escaped = copy.deepcopy(report)
            escaped['runtime']['evidence'][0]['path'] = '../outside.jsonl'
            failed_output = root / 'escaped.json'
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'owner', 'GITHUB_REPOSITORY_OWNER': 'owner'}), \
                 patch('scripts.ci.runtime_task.subprocess.check_output', return_value='a' * 40), \
                 patch('scripts.ci.runtime_task.verify_validation_files', return_value=escaped):
                with self.assertRaisesRegex(ValueError, 'escapes its trusted root'):
                    register_cli(args[:-1] + [str(failed_output)])
            self.assertEqual(v2_before, v2_path.read_bytes())
            self.assertEqual(archives_before, sorted(path.name for path in archive_root.iterdir()))
            self.assertFalse(failed_output.exists())

            raw_original = raw_path.read_bytes()
            symlink_target = external / 'raw-copy.jsonl'
            symlink_target.write_bytes(raw_original)
            raw_path.unlink()
            raw_path.symlink_to(symlink_target)
            failed_output = root / 'symlink.json'
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'owner', 'GITHUB_REPOSITORY_OWNER': 'owner'}), \
                 patch('scripts.ci.runtime_task.subprocess.check_output', return_value='a' * 40), \
                 patch('scripts.ci.runtime_task.verify_validation_files', return_value=report):
                with self.assertRaisesRegex(ValueError, 'symbolic link'):
                    register_cli(args[:-1] + [str(failed_output)])
            raw_path.unlink()
            raw_path.write_bytes(raw_original)
            self.assertEqual(v2_before, v2_path.read_bytes())
            self.assertEqual(archives_before, sorted(path.name for path in archive_root.iterdir()))
            self.assertFalse(failed_output.exists())

            raw_original = raw_path.read_bytes()
            raw_path.write_bytes(raw_original + b'tampered\n')
            failed_output = root / 'tampered.json'
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'owner', 'GITHUB_REPOSITORY_OWNER': 'owner'}), \
                 patch('scripts.ci.runtime_task.subprocess.check_output', return_value='a' * 40), \
                 patch('scripts.ci.runtime_task.verify_validation_files', return_value=report):
                with self.assertRaisesRegex(ValueError, 'source hash changed'):
                    register_cli(args[:-1] + [str(failed_output)])
            raw_path.write_bytes(raw_original)
            self.assertEqual(v2_before, v2_path.read_bytes())
            self.assertEqual(archives_before, sorted(path.name for path in archive_root.iterdir()))
            self.assertFalse(failed_output.exists())



if __name__ == '__main__':
    unittest.main()
