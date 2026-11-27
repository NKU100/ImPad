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
            self.assertFalse((root / 'acceptance-v2.json').exists())
            self.assertFalse((root / 'targets.json').exists())

    def test_verify_and_register_clis_do_not_expose_initial_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = ['--app-id', 'wechat', '--candidate', str(root / 'candidate.json'),
                      '--module-apk', str(root / 'module.apk'), '--regression', str(root / 'regression.json'),
                      '--smoke', str(root / 'smoke.json'), '--evidence-root', str(root),
                      '--acceptance', str(root / 'compatibility/wechat/acceptance-v2.json'), '--repository', str(root),
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
            self.assertFalse((root / 'compatibility/wechat/acceptance-v2.json').exists())
            self.assertFalse((root / 'register.json').exists())

    def test_verify_cli_emits_schema_two_without_internal_migration_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = {'schemaVersion': 2, 'outcome': 'VERIFIED', 'failedStage': None,
                      'candidate': {'appId': 'wechat'}, '_regressionBaselineObject': object()}
            args = ['--app-id', 'wechat', '--candidate', str(root / 'candidate.json'),
                    '--module-apk', str(root / 'module.apk'), '--regression', str(root / 'regression.json'),
                    '--smoke', str(root / 'smoke.json'), '--evidence-root', str(root),
                    '--acceptance', str(root / 'compatibility/wechat/acceptance-v2.json'),
                    '--repository', str(root), '--conclusion', 'success', '--output', str(root / 'verify.json')]
            with patch('scripts.ci.runtime_task.verify_validation_files', return_value=report):
                verify_cli(args)
            output = json.loads((root / 'verify.json').read_text())
            self.assertEqual(2, output['schemaVersion'])
            self.assertNotIn('_regressionBaselineObject', output)

    def test_grouped_stage_context_uses_v2_pool_without_loading_migration_inputs(self):
        from scripts.ci.semantic_regression_baseline import VerifiedRegressionPool
        from scripts.ci.grouped_static_regression import group_report, regression_selection

        case = {'kind': 'ACCEPTED_V2', 'recordId': 'sample-1', 'appId': 'wechat',
                'identity': {'versionCode': 100}, 'apkFiles': [{'role': 'base', 'sha256': 'a' * 64}]}
        raw = json.dumps({'schemaVersion': 2, 'appId': 'wechat', 'records': [
            {'recordId': 'sample-1', 'acceptedAt': '2026-01-01T00:00:00Z'}]}).encode()
        pool = VerifiedRegressionPool('wechat', (case,), raw, hashlib.sha256(raw).hexdigest(),
                                      Path('/unused'), '0' * 64)
        selection = regression_selection(pool)
        group = selection['selectedGroups'][0]
        row = {'groupId': group['groupId'], 'candidateRecordIds': ['sample-1'],
               'cacheMissRecordIds': ['sample-1'],
               'unavailable': [{'recordId': 'sample-1', 'error': '404'}],
               'status': 'SKIPPED', 'skipReason': 'APK_UNAVAILABLE'}
        report = group_report('wechat', pool.v2_sha256, selection, [row])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / 'results.json'
            report_path.write_text(json.dumps(report))
            regression = dict(report, status='PARTIAL', evidence={
                'path': str(report_path), 'sha256': hashlib.sha256(report_path.read_bytes()).hexdigest()})
            gate = {'regressionStatus': 'PARTIAL'}
            with patch('scripts.ci.semantic_regression_baseline.load_verified_regression_pool',
                       return_value=pool), \
                 patch('scripts.ci.grouped_static_regression.verify_grouped_regression',
                       return_value=report), \
                 patch('scripts.ci.semantic_validation.validate_semantic_stage_models',
                       return_value=gate), \
                 patch.object(VerifiedRegressionPool, 'assert_unchanged'):
                result, same_pool = __import__(
                    'scripts.ci.runtime_task', fromlist=['_semantic_stage_context']
                )._semantic_stage_context(
                    {'appId': 'wechat'}, {}, {'executionIdentity': {}}, regression, root,
                    acceptance_path=root / 'compatibility/wechat/acceptance-v2.json')
        self.assertIs(pool, same_pool)
        self.assertIs(gate, result)

    def test_semantic_context_rejects_legacy_per_record_reports(self):
        from scripts.ci.runtime_task import _semantic_stage_context

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, 'grouped results schema'):
                _semantic_stage_context(
                    {'appId': 'wechat'}, {}, {'executionIdentity': {}},
                    {'records': [{'recordId': 'legacy', 'status': 'PASSED'}]}, root,
                    acceptance_path=root / 'compatibility/wechat/acceptance-v2.json')

    def test_register_rejects_the_frozen_v1_acceptance_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            args = ['--app-id', 'wechat', '--candidate', str(root / 'candidate.json'),
                    '--module-apk', str(root / 'module.apk'), '--regression', str(root / 'results.json'),
                    '--smoke', str(root / 'smoke.json'), '--evidence-root', str(root),
                    '--acceptance', str(root / 'compatibility/wechat/acceptance.json'),
                    '--repository', str(root), '--conclusion', 'success',
                    '--expected-head', 'a' * 40, '--output', str(root / 'registered.json')]
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'owner', 'GITHUB_REPOSITORY_OWNER': 'owner'}), \
                 patch('scripts.ci.runtime_task.subprocess.check_output', return_value='a' * 40), \
                 self.assertRaisesRegex(ValueError, 'Acceptance path does not match'):
                register_cli(args)

    def test_register_revalidates_group_binding_and_archives_exact_evidence(self):
        from scripts.ci.grouped_static_regression import group_report, regression_baseline_binding, regression_selection
        from scripts.ci.semantic_regression_baseline import load_verified_regression_pool
        from scripts.ci.acceptance import SEMANTIC_ACCEPTANCE_FILENAME

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / 'checkout'
            root.mkdir()
            shutil.copytree(Path(__file__).resolve().parents[1] / 'compatibility', root / 'compatibility')
            table_path = root / 'compatibility/wechat/acceptance-v2.json'
            original_table = table_path.read_bytes()
            frozen_root = root / 'compatibility/semantic-migration'
            frozen_bytes = {path.relative_to(frozen_root).as_posix(): path.read_bytes()
                            for path in frozen_root.rglob('*') if path.is_file()}
            original_records = json.loads(original_table)['records']
            source_record = original_records[0]
            source_inputs = [row['sha256'] for row in source_record['apkFiles']]
            source_execution = source_record['executionSha256']
            retained = [row for row in original_records if not (
                [item['sha256'] for item in row['apkFiles']] == source_inputs
                and row['executionSha256'] == source_execution)]
            table_path.write_text(json.dumps({'schemaVersion': 2, 'appId': 'wechat',
                                              'records': retained}, indent=2) + '\n')
            pool = load_verified_regression_pool(root, 'wechat')
            static = copy.deepcopy(source_record['staticReport'])
            candidate_root = root / 'candidate-artifact'
            candidate_root.mkdir()
            static_path = candidate_root / 'static.json'
            static_path.write_text(json.dumps(static))
            candidate = {
                'schemaVersion': 2, 'status': 'PREPARED', 'appId': 'wechat',
                'identity': copy.deepcopy(source_record['identity']),
                'sourceUrl': source_record['source']['url'],
                'apkFiles': copy.deepcopy(source_record['apkFiles']),
                'ruleCatalog': copy.deepcopy(static['ruleCatalog']),
                'ruleCatalogSha256': static['ruleCatalog']['ruleCatalogSha256'],
                'snapshot': copy.deepcopy(static['snapshot']),
                'expectedPlan': copy.deepcopy(source_record['completePlan']),
                'expectedExecutionSha256': source_record['executionSha256'],
                'recognition': copy.deepcopy(source_record['recognition']),
                'recognitionSha256': source_record['recognitionSha256'],
                'staticReport': 'static.json',
                'staticReportSha256': hashlib.sha256(static_path.read_bytes()).hexdigest(),
            }
            candidate_path = candidate_root / 'candidate-report.json'
            candidate_path.write_text(json.dumps(candidate))
            selection = regression_selection(pool)
            groups = []
            for group in selection['selectedGroups']:
                ids = [case['recordId'] for case in group['cases']]
                groups.append({'groupId': group['groupId'], 'candidateRecordIds': ids,
                               'cacheMissRecordIds': ids,
                               'unavailable': [{'recordId': value, 'error': 'HTTP 404'} for value in ids],
                               'status': 'SKIPPED', 'skipReason': 'APK_UNAVAILABLE'})
            grouped = group_report('wechat', pool.v2_sha256, selection, groups)
            regression_path = root / 'regression/results.json'
            regression_path.parent.mkdir()
            regression_path.write_text(json.dumps(grouped))
            evidence_root = root / 'runtime-evidence'
            evidence_root.mkdir()
            smoke_path = evidence_root / 'smoke.json'
            raw_path = evidence_root / 'raw.jsonl'
            smoke_path.write_text('{"status":"RUNTIME_SMOKE_VERIFIED"}\n')
            raw_path.write_text('{"event":"verified"}\n')
            smoke_sha = hashlib.sha256(smoke_path.read_bytes()).hexdigest()
            raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            module = copy.deepcopy(source_record['module'])
            report = {
                'schemaVersion': 2, 'outcome': 'VERIFIED', 'failedStage': None,
                'candidate': candidate, 'module': module, 'static': static,
                'regression': dict(grouped, status='PARTIAL', evidence={
                    'path': str(regression_path),
                    'sha256': hashlib.sha256(regression_path.read_bytes()).hexdigest()}),
                'regressionBaseline': regression_baseline_binding(pool),
                'regressionRecordIds': [row['groupId'] for row in groups],
                'v2SnapshotSha256': pool.v2_sha256,
                'stageEvidence': {
                    'static': {'path': 'static.json', 'sha256': candidate['staticReportSha256']},
                    'regression': {'path': str(regression_path),
                                   'sha256': hashlib.sha256(regression_path.read_bytes()).hexdigest()},
                    'runtime': {'path': str(smoke_path), 'sha256': smoke_sha},
                },
                'runtime': {'evidenceReport': {'path': 'smoke.json', 'sha256': smoke_sha},
                            'evidence': [{'path': 'raw.jsonl', 'sha256': raw_sha}]},
                'inputPaths': {'repository': str(root), 'candidate': str(candidate_path),
                               'moduleApk': str(root / 'module.apk'),
                               'regression': str(regression_path), 'smoke': str(smoke_path),
                               'evidenceRoot': str(evidence_root)},
            }
            (root / 'module.apk').write_bytes(b'module')
            output_path = root / 'registered.json'
            args = ['--app-id', 'wechat', '--candidate', str(candidate_path),
                    '--module-apk', str(root / 'module.apk'), '--regression', str(regression_path),
                    '--smoke', str(smoke_path), '--evidence-root', str(evidence_root),
                    '--acceptance', str(table_path), '--repository', str(root),
                    '--conclusion', 'success', '--expected-head', 'a' * 40,
                    '--output', str(output_path)]
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'owner', 'GITHUB_REPOSITORY_OWNER': 'owner'}), \
                 patch('scripts.ci.runtime_task.subprocess.check_output', return_value='a' * 40), \
                 patch('scripts.ci.runtime_task.verify_validation_files', return_value=report) as verifier:
                register_cli(args)
            self.assertEqual(2, verifier.call_count)
            registered = json.loads(output_path.read_text())
            self.assertEqual('compatibility/wechat/' + SEMANTIC_ACCEPTANCE_FILENAME,
                             registered['acceptancePath'])
            table = json.loads(table_path.read_text())
            self.assertEqual(1, len(table['records']) - len(retained))
            new_record = table['records'][-1]
            self.assertNotIn('migrationBundle', new_record)
            self.assertEqual(report['regressionBaseline'], new_record['regressionBaseline'])
            self.assertEqual([row['groupId'] for row in groups], new_record['skippedRegressionGroupIds'])
            from scripts.ci.acceptance import assert_evidence_archive_unchanged
            assert_evidence_archive_unchanged(root, new_record['evidenceArchive'])
            self.assertEqual(frozen_bytes,
                             {path.relative_to(frozen_root).as_posix(): path.read_bytes()
                              for path in frozen_root.rglob('*') if path.is_file()})


if __name__ == '__main__':
    unittest.main()
