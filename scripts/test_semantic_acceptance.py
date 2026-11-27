import copy
import hashlib
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from scripts.ci.acceptance import (build_semantic_acceptance_record,
                                   archive_verified_evidence,
                                   read_semantic_acceptance_table,
                                   validate_semantic_acceptance_table,
                                   write_semantic_acceptance_table)
ROOT = Path(__file__).resolve().parents[1]


class SemanticAcceptanceTest(unittest.TestCase):
    def _change_record_id(self, record, record_id):
        root = record['evidenceArchive']['path']
        new_root = f'compatibility/{record["appId"]}/acceptance-evidence/{record_id}'
        old_regression = self.root / record['rawStageEvidence']['regression']['path']
        regression_bytes = old_regression.read_bytes() if old_regression.is_file() else None
        record['recordId'] = record_id
        record['evidenceArchive']['path'] = new_root
        record['evidenceArchive']['manifestPath'] = new_root + '/manifest.json'
        for item in record['evidenceArchive']['files']:
            item['path'] = item['path'].replace(root, new_root, 1)
        for key in ('static', 'regression', 'runtime'):
            record['rawStageEvidence'][key]['path'] = record['rawStageEvidence'][key]['path'].replace(root, new_root, 1)
        for item in record['rawStageEvidence']['files']:
            item['path'] = item['path'].replace(root, new_root, 1)
        if regression_bytes is not None:
            new_regression = self.root / record['rawStageEvidence']['regression']['path']
            new_regression.parent.mkdir(parents=True, exist_ok=True)
            new_regression.write_bytes(regression_bytes)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.record = copy.deepcopy(json.loads(
            (ROOT / 'compatibility/wechat/acceptance-v2.json').read_text())['records'][0])
        original_regression = ROOT / self.record['rawStageEvidence']['regression']['path']
        archived_regression = original_regression.read_bytes()
        self._change_record_id(self.record, 'semantic-record-1')
        self.record['evidenceArchive']['files'] = self.record['evidenceArchive']['files'][:4]
        self.record['rawStageEvidence']['files'] = self.record['rawStageEvidence']['files'][:1]
        self.static = self.record['staticReport']
        self.apk_files = self.record['apkFiles']
        self.recognition = self.record['recognition']
        self.regression_baseline = self.record['regressionBaseline']
        self.evidence_archive = self.record['evidenceArchive']
        self.bundle = SimpleNamespace(
            history_sha256=self.record['migrationBundle']['historySha256'],
            expectations_sha256=self.record['migrationBundle']['expectationsSha256'])
        self.root.joinpath('compatibility/wechat/acceptance-evidence').mkdir(parents=True)
        self.root.joinpath('compatibility/wechat/acceptance.json').write_text('{}\n')
        regression_path = self.root / self.record['rawStageEvidence']['regression']['path']
        regression_path.parent.mkdir(parents=True, exist_ok=True)
        regression_path.write_bytes(archived_regression)

    def test_schema2_table_preserves_complete_semantic_and_evidence_identity(self):
        table = {'schemaVersion': 2, 'appId': 'wechat', 'records': [self.record]}
        records = validate_semantic_acceptance_table(table, 'wechat', repository=self.root)
        self.assertEqual(self.record, records[0])

    def test_schema2_table_allows_same_apk_under_distinct_execution_identity(self):
        from scripts.ci.acceptance import validate_semantic_acceptance_table

        second = copy.deepcopy(self.record)
        self._change_record_id(second, 'semantic-record-2')
        second['staticReport']['executionIdentity']['sourceTreeSha256'] = '9' * 64
        from scripts.ci.semantic_protocol import execution_identity_sha256
        second['staticReport']['executionIdentity']['cacheIdentitySha256'] = execution_identity_sha256(
            second['staticReport']['executionIdentity'])
        second['module']['sourceTreeSha256'] = '9' * 64
        second['module']['dexkit'] = copy.deepcopy(second['staticReport']['executionIdentity'])

        records = validate_semantic_acceptance_table(
            {'schemaVersion': 2, 'appId': 'wechat', 'records': [self.record, second]},
            'wechat', repository=self.root)

        self.assertEqual(['semantic-record-1', 'semantic-record-2'],
                         [record['recordId'] for record in records])

    def test_schema2_table_rejects_same_apk_and_execution_identity_twice(self):
        duplicate = copy.deepcopy(self.record)
        self._change_record_id(duplicate, 'semantic-record-duplicate')

        with self.assertRaisesRegex(ValueError, 'duplicate APK input and execution identity'):
            validate_semantic_acceptance_table(
                {'schemaVersion': 2, 'appId': 'wechat', 'records': [self.record, duplicate]},
                'wechat', repository=self.root)

    def test_draft_carries_full_plan_recognition_apk_module_raw_evidence_and_bundle(self):
        candidate = {'appId': 'wechat', 'identity': self.record['identity'],
                     'apkFiles': self.record['apkFiles'], 'expectedPlan': self.record['completePlan'],
                     'expectedExecutionSha256': self.record['executionSha256'],
                     'recognition': self.record['recognition'],
                     'recognitionSha256': self.record['recognitionSha256'],
                     'sourceUrl': self.record['source']['url']}
        report = {'schemaVersion': 2, 'outcome': 'VERIFIED', 'candidate': candidate,
                  'module': self.record['module'], 'static': self.static,
                  'stageEvidence': {name: self.record['rawStageEvidence'][name]
                                    for name in ('static', 'regression', 'runtime')},
                  'runtime': {'evidence': self.record['rawStageEvidence']['files']},
                  'migrationBundle': self.record['migrationBundle'],
                  'regressionBaseline': self.regression_baseline,
                  'evidenceArchive': self.evidence_archive}
        draft = build_semantic_acceptance_record(report)
        self.assertEqual(self.record['completePlan'], draft['completePlan'])
        self.assertEqual(self.record['executionSha256'], draft['executionSha256'])
        self.assertEqual(self.record['recognition'], draft['recognition'])
        self.assertEqual(self.record['apkFiles'], draft['apkFiles'])
        self.assertEqual({key: self.record['source'][key] for key in ('type', 'url')}, draft['source'])
        self.assertEqual(self.record['module'], draft['module'])
        self.assertEqual(self.record['rawStageEvidence'], draft['rawStageEvidence'])
        self.assertEqual(self.record['migrationBundle'], draft['migrationBundle'])

    def test_registration_records_skipped_regression_ids_for_each_app(self):
        for app_id in ('wechat', 'qq', 'wecom'):
            with self.subTest(app_id=app_id):
                table = json.loads((ROOT / f'compatibility/{app_id}/acceptance-v2.json').read_text())
                accepted = next(row for row in table['records']
                                if len(row['regressionBaseline']['regressionRecordIds']) > 1)
                record_id = accepted['regressionBaseline']['regressionRecordIds'][0]
                candidate = {key: accepted[key] for key in
                             ('appId', 'identity', 'apkFiles', 'recognition', 'recognitionSha256')}
                candidate.update(expectedPlan=accepted['completePlan'],
                                 expectedExecutionSha256=accepted['executionSha256'],
                                 sourceUrl=accepted['source']['url'])
                stage = accepted['rawStageEvidence']
                validation = {'schemaVersion': 2, 'outcome': 'VERIFIED', 'candidate': candidate,
                              'module': accepted['module'], 'static': accepted['staticReport'],
                              'stageEvidence': {key: stage[key] for key in ('static', 'regression', 'runtime')},
                              'runtime': {'evidence': stage['files']}, 'migrationBundle': accepted['migrationBundle'],
                              'regressionBaseline': accepted['regressionBaseline'],
                              'evidenceArchive': accepted['evidenceArchive'],
                              'regression': {'status': 'PARTIAL', 'records': json.loads(
                                  (ROOT / accepted['rawStageEvidence']['regression']['path']).read_text())}}
                ids = accepted['regressionBaseline']['regressionRecordIds']
                rows = validation['regression']['records']
                skipped_row = next(row for row in rows if row['recordId'] == record_id)
                skipped_row.update(status='SKIPPED', skipReason='APK_UNAVAILABLE', error='HTTP 404')
                skipped_row.pop('staticEvidence', None)
                draft = build_semantic_acceptance_record(validation)
                self.assertEqual([record_id], draft['skippedRegressionRecordIds'])
                reference = draft['rawStageEvidence']['regression']
                report = self.root / reference['path']
                report.parent.mkdir(parents=True)
                report.write_text(json.dumps(rows))
                digest = hashlib.sha256(report.read_bytes()).hexdigest()
                reference['sha256'] = draft['evidenceArchive']['files'][1]['sha256'] = digest
                draft_table = dict(table, records=[draft])
                self.assertEqual([draft], validate_semantic_acceptance_table(draft_table, app_id, self.root))
                draft['skippedRegressionRecordIds'] = ['unknown-record']
                with self.assertRaisesRegex(ValueError, 'skipped regression'):
                    validate_semantic_acceptance_table(draft_table, app_id, self.root)

                draft['skippedRegressionRecordIds'] = [ids[1]]
                with self.assertRaisesRegex(ValueError, 'archived.*regression'):
                    validate_semantic_acceptance_table(draft_table, app_id, self.root)

    def test_grouped_acceptance_builds_without_migration_bundle_and_validates_archived_report(self):
        grouped = copy.deepcopy(self.record)
        grouped.pop('migrationBundle')
        candidate_ids = ['accepted-one', 'accepted-two']
        grouped['regressionBaseline'] = {
            'appId': 'wechat', 'v2SnapshotSha256': 'a' * 64, 'groupingVersion': 1,
            'selectedGroups': [{'groupId': 'group-one', 'candidateRecordIds': candidate_ids}],
            'excludedGroupIds': ['group-excluded'],
        }
        report = {'schemaVersion': 1, 'groupingVersion': 1, 'appId': 'wechat',
                  'acceptanceSha256': 'a' * 64, 'excludedGroupIds': ['group-excluded'],
                  'groups': [{'groupId': 'group-one', 'candidateRecordIds': candidate_ids,
                              'cacheMissRecordIds': candidate_ids,
                              'unavailable': [{'recordId': 'accepted-one', 'error': '404'},
                                              {'recordId': 'accepted-two', 'error': '404'}],
                              'status': 'SKIPPED', 'skipReason': 'APK_UNAVAILABLE'}]}
        validation = {'schemaVersion': 2, 'outcome': 'VERIFIED',
                      'candidate': {'appId': grouped['appId'], 'identity': grouped['identity'],
                                    'apkFiles': grouped['apkFiles'], 'expectedPlan': grouped['completePlan'],
                                    'expectedExecutionSha256': grouped['executionSha256'],
                                    'recognition': grouped['recognition'],
                                    'recognitionSha256': grouped['recognitionSha256'],
                                    'sourceUrl': grouped['source']['url']},
                      'module': grouped['module'], 'static': grouped['staticReport'],
                      'stageEvidence': {name: grouped['rawStageEvidence'][name]
                                        for name in ('static', 'regression', 'runtime')},
                      'runtime': {'evidence': grouped['rawStageEvidence']['files']},
                      'regressionBaseline': grouped['regressionBaseline'],
                      'regression': {'status': 'PARTIAL', 'groups': report['groups']},
                      'evidenceArchive': grouped['evidenceArchive']}
        draft = build_semantic_acceptance_record(validation)
        self.assertNotIn('migrationBundle', draft)
        self.assertEqual(['group-one'], draft['skippedRegressionGroupIds'])
        self.assertEqual(grouped['regressionBaseline'], draft['regressionBaseline'])
        regression_ref = draft['rawStageEvidence']['regression']
        archived = self.root / regression_ref['path']
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_text(json.dumps(report))
        digest = hashlib.sha256(archived.read_bytes()).hexdigest()
        regression_ref['sha256'] = draft['evidenceArchive']['files'][1]['sha256'] = digest
        table = {'schemaVersion': 2, 'appId': 'wechat', 'records': [draft]}
        self.assertEqual([draft], validate_semantic_acceptance_table(table, 'wechat', self.root))
        report['groups'][0]['groupId'] = 'wrong-group'
        archived.write_text(json.dumps(report))
        changed = hashlib.sha256(archived.read_bytes()).hexdigest()
        regression_ref['sha256'] = draft['evidenceArchive']['files'][1]['sha256'] = changed
        with self.assertRaisesRegex(ValueError, 'archived grouped regression'):
            validate_semantic_acceptance_table(table, 'wechat', self.root)
        report['groups'][0]['groupId'] = 'group-one'
        draft['skippedRegressionGroupIds'] = ['group-excluded']
        with self.assertRaisesRegex(ValueError, 'skipped regression groups'):
            validate_semantic_acceptance_table(table, 'wechat', self.root)

    def test_legacy_v2_record_uses_archived_metadata_without_migration_directory(self):
        self.assertFalse((self.root / 'compatibility/semantic-migration').exists())
        records = validate_semantic_acceptance_table(
            {'schemaVersion': 2, 'appId': 'wechat', 'records': [self.record]},
            'wechat', self.root)
        self.assertEqual([self.record], records)

    def test_legacy_v2_record_rejects_archived_regression_with_different_provenance(self):
        damaged = copy.deepcopy(self.record)
        regression_path = self.root / damaged['rawStageEvidence']['regression']['path']
        rows = json.loads(regression_path.read_bytes())
        rows[0]['baselineSha256'] = '0' * 64
        raw = json.dumps(rows).encode()
        regression_path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        damaged['rawStageEvidence']['regression']['sha256'] = digest
        damaged['evidenceArchive']['files'][1]['sha256'] = digest
        with self.assertRaisesRegex(ValueError, 'archived regression provenance'):
            validate_semantic_acceptance_table(
                {'schemaVersion': 2, 'appId': 'wechat', 'records': [damaged]},
                'wechat', self.root)

    def test_grouped_archive_preserves_passed_static_reports_after_runtime_files(self):
        import uuid

        app_root = self.root / 'compatibility/wechat'
        app_root.mkdir(parents=True, exist_ok=True)
        candidate_root = self.root / 'candidate'
        candidate_root.mkdir()
        regression_root = self.root / 'regression'
        regression_root.mkdir()
        raw_root = candidate_root / 'raw'
        raw_root.mkdir()

        candidate = {'appId': 'wechat', 'identity': self.record['identity'],
                     'apkFiles': self.record['apkFiles'],
                     'expectedPlan': self.record['completePlan'],
                     'expectedExecutionSha256': self.record['executionSha256'],
                     'recognition': self.record['recognition'],
                     'recognitionSha256': self.record['recognitionSha256'],
                     'sourceUrl': self.record['source']['url'],
                     'staticReport': 'static.json'}
        static_path = candidate_root / 'static.json'
        static_path.write_text(json.dumps(self.static))
        smoke_path = candidate_root / 'smoke.json'
        smoke_path.write_text('{}')
        raw_path = raw_root / 'logcat.txt'
        raw_path.write_text('runtime evidence')
        group_static_path = regression_root / 'group-static.json'
        group_static_path.write_text(json.dumps(self.static))
        group_static_sha = hashlib.sha256(group_static_path.read_bytes()).hexdigest()
        report = {'schemaVersion': 1, 'groupingVersion': 1, 'appId': 'wechat',
                  'acceptanceSha256': 'a' * 64, 'excludedGroupIds': [],
                  'groups': [{'groupId': 'group-one', 'candidateRecordIds': ['old-record'],
                              'cacheMissRecordIds': [], 'unavailable': [], 'status': 'PASSED',
                              'recordId': 'old-record', 'apkSource': 'cache',
                              'staticEvidence': {'path': group_static_path.name,
                                                 'sha256': group_static_sha}}]}
        regression_path = regression_root / 'results.json'
        regression_path.write_text(json.dumps(report))
        write_candidate = candidate_root / 'candidate.json'
        write_candidate.write_text(json.dumps(candidate))
        smoke_sha = hashlib.sha256(smoke_path.read_bytes()).hexdigest()
        raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        validation = {'schemaVersion': 2, 'outcome': 'VERIFIED', 'candidate': candidate,
                      'module': self.record['module'], 'static': self.static,
                      'runtime': {'evidenceReport': {'path': 'smoke.json', 'sha256': smoke_sha},
                                  'evidence': [{'path': 'raw/logcat.txt', 'sha256': raw_sha}]},
                      'stageEvidence': {
                          'static': {'path': str(static_path),
                                     'sha256': hashlib.sha256(static_path.read_bytes()).hexdigest()},
                          'regression': {'path': str(regression_path),
                                         'sha256': hashlib.sha256(regression_path.read_bytes()).hexdigest()},
                          'runtime': {'path': str(smoke_path), 'sha256': smoke_sha}},
                      'inputPaths': {'repository': str(self.root),
                                     'candidate': str(write_candidate),
                                     'regression': str(regression_path),
                                     'smoke': str(smoke_path),
                                     'evidenceRoot': str(candidate_root)},
                      'regression': {'status': 'PASSED', 'groups': report['groups']},
                      'regressionBaseline': {
                          'appId': 'wechat', 'v2SnapshotSha256': 'a' * 64,
                          'groupingVersion': 1,
                          'selectedGroups': [{'groupId': 'group-one',
                                              'candidateRecordIds': ['old-record']}],
                          'excludedGroupIds': []}}
        record_id = str(uuid.uuid4())
        archive, archive_paths = archive_verified_evidence(validation, self.root, record_id)
        self.assertEqual(5, len(archive['files']))
        self.assertTrue(archive['files'][3]['path'].endswith('raw-0000-logcat.txt'))
        self.assertTrue(archive['files'][4]['path'].endswith('group-static-0000-group-static.json'))
        draft = build_semantic_acceptance_record(
            validation, evidence_archive=archive, record_id=record_id)
        table = {'schemaVersion': 2, 'appId': 'wechat', 'records': [draft]}
        self.assertEqual([draft], validate_semantic_acceptance_table(table, 'wechat', self.root))
        self.assertIn(archive['files'][4]['path'], archive_paths)
        archived_static = self.root / archive['files'][4]['path']
        archived_static.write_text(archived_static.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'archived grouped static evidence hash changed'):
            validate_semantic_acceptance_table(table, 'wechat', self.root)

    def test_draft_normalizes_absolute_stage_paths_under_trusted_roots(self):
        candidate = {'appId': 'wechat', 'identity': self.record['identity'],
                     'apkFiles': self.record['apkFiles'], 'expectedPlan': self.record['completePlan'],
                     'expectedExecutionSha256': self.record['executionSha256'],
                     'recognition': self.record['recognition'],
                     'recognitionSha256': self.record['recognitionSha256'],
                     'sourceUrl': self.record['source']['url']}
        report = {'schemaVersion': 2, 'outcome': 'VERIFIED', 'candidate': candidate,
                  'module': self.record['module'], 'static': self.static,
                  'stageEvidence': {'static': self.record['rawStageEvidence']['static'],
                                    'regression': {'path': str(self.root / 'reports/regression.json'),
                                                   'sha256': '3' * 64},
                                    'runtime': {'path': str(self.root / 'smoke.json'),
                                                'sha256': '4' * 64}},
                  'runtime': {'evidenceReport': {'path': 'smoke-report.json', 'sha256': '4' * 64},
                              'evidence': [{'path': 'raw/logcat.txt', 'sha256': '5' * 64}]},
                  'migrationBundle': self.record['migrationBundle'],
                  'regressionBaseline': self.regression_baseline,
                  'evidenceArchive': self.evidence_archive,
                  'inputPaths': {'repository': str(self.root),
                                 'evidenceRoot': str(self.root / 'runtime-evidence')}}
        draft = build_semantic_acceptance_record(report)
        self.assertEqual(self.evidence_archive['files'][1]['path'],
                         draft['rawStageEvidence']['regression']['path'])
        self.assertEqual(self.evidence_archive['files'][2]['path'],
                         draft['rawStageEvidence']['runtime']['path'])
        self.assertEqual(self.evidence_archive['files'][3]['path'],
                         draft['rawStageEvidence']['files'][0]['path'])

    def test_input_hashes_must_match_static_report_in_order_and_count(self):
        mutations = []
        extra = copy.deepcopy(self.record)
        extra['apkFiles'].append({'path': 'apk-inputs/extra.apk', 'role': 'split', 'sha256': 'c' * 64})
        mutations.append(('extra split', extra, 'APK input list differs'))
        omitted = copy.deepcopy(self.record)
        omitted['apkFiles'].pop()
        mutations.append(('omitted base', omitted, 'APK input identity is incomplete'))
        changed = copy.deepcopy(self.record)
        changed['apkFiles'][0]['sha256'] = 'c' * 64
        mutations.append(('changed base hash', changed, 'APK input identity is incomplete'))
        for label, record, error in mutations:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, error):
                validate_semantic_acceptance_table(
                    {'schemaVersion': 2, 'appId': 'wechat', 'records': [record]},
                    'wechat', repository=self.root)

    def test_writer_rejects_apk_input_mismatch_without_creating_table(self):
        path = self.root / 'acceptance-v2.json'
        malformed = copy.deepcopy(self.record)
        malformed['apkFiles'].append({'path': 'apk-inputs/extra.apk', 'role': 'split',
                                      'sha256': 'c' * 64})
        with self.assertRaises(ValueError):
            write_semantic_acceptance_table(path, malformed, 'wechat',
                                            expected_existing_bytes=None,
                                            repository=self.root)
        self.assertFalse(path.exists())

    def test_schema2_record_rejects_static_inputs_outside_captured_manifests(self):
        damaged = copy.deepcopy(self.record)
        damaged['staticReport']['inputHashes'].append(
            copy.deepcopy(damaged['staticReport']['inputHashes'][0]))
        with self.assertRaisesRegex(ValueError, 'APK input hashes differ from captured manifests'):
            validate_semantic_acceptance_table(
                {'schemaVersion': 2, 'appId': 'wechat', 'records': [damaged]},
                'wechat', repository=self.root)

    def test_new_record_preserves_verified_source_url_and_archive_pin(self):
        candidate = {'appId': 'wechat', 'identity': self.record['identity'],
                     'apkFiles': self.record['apkFiles'], 'expectedPlan': self.record['completePlan'],
                     'expectedExecutionSha256': self.record['executionSha256'],
                     'recognition': self.record['recognition'],
                     'recognitionSha256': self.record['recognitionSha256'],
                     'sourceUrl': self.record['source']['url'],
                     'sourceArchiveSha256': 'e' * 64}
        report = {'schemaVersion': 2, 'outcome': 'VERIFIED', 'candidate': candidate,
                  'module': self.record['module'], 'static': self.static,
                  'stageEvidence': {name: self.record['rawStageEvidence'][name]
                                    for name in ('static', 'regression', 'runtime')},
                  'runtime': {'evidence': self.record['rawStageEvidence']['files']},
                  'migrationBundle': self.record['migrationBundle'],
                  'regressionBaseline': self.regression_baseline,
                  'evidenceArchive': self.evidence_archive}
        draft = build_semantic_acceptance_record(report)
        self.assertEqual({'type': 'official', 'url': candidate['sourceUrl'],
                          'archiveSha256': 'e' * 64}, draft['source'])

    def test_new_record_rejects_nonofficial_source_without_signed_archive_pin(self):
        candidate = {'appId': 'wechat', 'identity': self.record['identity'],
                     'apkFiles': self.record['apkFiles'], 'expectedPlan': self.record['completePlan'],
                     'expectedExecutionSha256': self.record['executionSha256'],
                     'recognition': self.record['recognition'],
                     'recognitionSha256': self.record['recognitionSha256'],
                     'sourceUrl': 'https://mirror.example/wechat.apk'}
        report = {'schemaVersion': 2, 'outcome': 'VERIFIED', 'candidate': candidate,
                  'module': self.record['module'], 'static': self.static,
                  'stageEvidence': {name: self.record['rawStageEvidence'][name]
                                    for name in ('static', 'regression', 'runtime')},
                  'runtime': {'evidence': self.record['rawStageEvidence']['files']},
                  'migrationBundle': self.record['migrationBundle'],
                  'regressionBaseline': self.regression_baseline,
                  'evidenceArchive': self.evidence_archive}
        with self.assertRaisesRegex(ValueError, 'archive hash'):
            build_semantic_acceptance_record(report)
        candidate['sourceArchiveSha256'] = 'e' * 64
        draft = build_semantic_acceptance_record(report)
        self.assertEqual({'type': 'signed-download', 'url': candidate['sourceUrl'],
                          'archiveSha256': 'e' * 64}, draft['source'])

    def test_schema2_table_rejects_forged_execution_recognition_and_duplicate_apk_inputs(self):
        for field, value in (('executionSha256', '0' * 64), ('recognitionSha256', '0' * 64)):
            damaged = copy.deepcopy(self.record)
            damaged[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_semantic_acceptance_table({'schemaVersion': 2, 'appId': 'wechat', 'records': [damaged]},
                                                   'wechat', repository=self.root)
        duplicate = copy.deepcopy(self.record)
        self._change_record_id(duplicate, 'semantic-record-2')
        with self.assertRaisesRegex(ValueError, 'duplicate APK input'):
            validate_semantic_acceptance_table(
                {'schemaVersion': 2, 'appId': 'wechat', 'records': [self.record, duplicate]},
                'wechat', repository=self.root)

    def test_schema2_table_rejects_module_fingerprint_or_frozen_bundle_drift(self):
        damaged = copy.deepcopy(self.record)
        damaged['module']['sourceTreeSha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'module fingerprint'):
            validate_semantic_acceptance_table(
                {'schemaVersion': 2, 'appId': 'wechat', 'records': [damaged]},
                'wechat', repository=self.root)
        damaged = copy.deepcopy(self.record)
        damaged['migrationBundle']['expectationsSha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'baseline differs from its migration'):
            validate_semantic_acceptance_table(
                {'schemaVersion': 2, 'appId': 'wechat', 'records': [damaged]},
                'wechat', repository=self.root)

    def test_atomic_writer_detects_concurrent_v2_change_without_overwriting(self):
        path = self.root / 'acceptance-v2.json'
        expected = None
        path.write_text('{"schemaVersion":2,"appId":"wechat","records":[]}\n')
        current = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'changed during registration'):
            write_semantic_acceptance_table(path, self.record, 'wechat', expected_existing_bytes=expected,
                                            repository=self.root)
        self.assertEqual(current, path.read_bytes())

    def test_atomic_writer_writes_new_schema2_table_without_touching_legacy_file(self):
        legacy = self.root / 'compatibility/wechat/acceptance.json'
        legacy_before = legacy.read_bytes()
        path = self.root / 'acceptance-v2.json'
        write_semantic_acceptance_table(path, self.record, 'wechat', expected_existing_bytes=None,
                                        repository=self.root)
        table = json.loads(path.read_text())
        self.assertEqual(2, table['schemaVersion'])
        self.assertEqual(self.record, table['records'][0])
        self.assertEqual(legacy_before, legacy.read_bytes())
        v2_before = path.read_bytes()
        duplicate = copy.deepcopy(self.record)
        self._change_record_id(duplicate, 'semantic-record-duplicate')
        with self.assertRaisesRegex(ValueError, 'duplicate APK input'):
            write_semantic_acceptance_table(path, duplicate, 'wechat',
                                            expected_existing_bytes=v2_before,
                                            repository=self.root)
        self.assertEqual(v2_before, path.read_bytes())

    def test_existing_v2_table_is_validated_before_registration(self):
        path = self.root / 'acceptance-v2.json'
        path.write_text('{invalid')
        with self.assertRaisesRegex(ValueError, 'unreadable'):
            read_semantic_acceptance_table(path, 'wechat', repository=self.root)
        self.assertEqual('{invalid', path.read_text())


if __name__ == '__main__':
    unittest.main()
