import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.ci.semantic_migration import load_migration_bundle
from scripts.ci.acceptance import (build_semantic_acceptance_record,
                                   read_semantic_acceptance_table,
                                   validate_semantic_acceptance_table,
                                   write_semantic_acceptance_table)
from scripts.ci.semantic_protocol import plan_execution_sha256, snapshot_sha256
from scripts.test_semantic_migration import ROOT, SemanticMigrationTest


class SemanticAcceptanceTest(unittest.TestCase):
    @staticmethod
    def _change_record_id(record, record_id):
        root = record['evidenceArchive']['path']
        new_root = f'compatibility/{record["appId"]}/acceptance-evidence/{record_id}'
        record['recordId'] = record_id
        record['evidenceArchive']['path'] = new_root
        record['evidenceArchive']['manifestPath'] = new_root + '/manifest.json'
        for item in record['evidenceArchive']['files']:
            item['path'] = item['path'].replace(root, new_root, 1)
        for key in ('static', 'regression', 'runtime'):
            record['rawStageEvidence'][key]['path'] = record['rawStageEvidence'][key]['path'].replace(root, new_root, 1)
        for item in record['rawStageEvidence']['files']:
            item['path'] = item['path'].replace(root, new_root, 1)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / 'compatibility', self.root / 'compatibility')
        self.bundle = load_migration_bundle(self.root)
        profile = next(row for row in self.bundle.profiles if row['appId'] == 'wechat')
        self.static = SemanticMigrationTest.report(SemanticMigrationTest('runTest'), profile)
        input_hashes = [profile['apkInputSha256'][0], 'a' * 64, 'b' * 64]
        self.static['inputHashes'] = [
            {'path': f'/candidate/input-{index}.apk', 'sha256': digest}
            for index, digest in enumerate(input_hashes)
        ]
        snapshot = self.static['snapshot']
        loader = snapshot['loaders'][0]
        loader['dexSha256'].extend(input_hashes[1:])
        snapshot['archives'].extend(
            {'loaderId': loader['id'], 'containerIndex': index, 'kind': 'DEX',
             'containerSha256': digest, 'dexSha256': [digest]}
            for index, digest in enumerate(input_hashes[1:], start=len(snapshot['archives']))
        )
        snapshot['contentSha256'] = snapshot_sha256(snapshot)
        plan = self.static['selection']['plan']
        plan['snapshotSha256'] = snapshot['contentSha256']
        execution_sha = plan_execution_sha256(plan)
        self.static['selection']['executionSha256'] = execution_sha
        for recognizer in self.static['recognizers']:
            for envelope in recognizer['plans']:
                envelope['plan'] = copy.deepcopy(plan)
                envelope['executionSha256'] = execution_sha
        self.apk_files = [
            {'path': f'apk-inputs/input-{index}.apk', 'role': 'base' if index == 0 else 'split',
             'sha256': digest}
            for index, digest in enumerate(input_hashes)
        ]
        self.recognition = {
            'appId': 'wechat',
            'evaluatedRecognizerIds': self.static['evaluatedRecognizerIds'],
            'ruleCatalogSha256': self.static['ruleCatalog']['ruleCatalogSha256'],
        }
        from scripts.ci.semantic_validation import _recognition
        self.recognition, recognition_sha = _recognition(self.static)
        self.record = {
            'recordId': 'semantic-record-1', 'schemaVersion': 2, 'appId': 'wechat',
            'status': 'VERIFIED', 'identity': profile['identity'],
            'apkFiles': self.apk_files,
            'completePlan': self.static['selection']['plan'],
            'executionSha256': execution_sha,
            'recognition': self.recognition, 'recognitionSha256': recognition_sha,
            'module': {'schemaVersion': 2, 'sha256': '1' * 64,
                       'sourceTreeSha256': self.static['executionIdentity']['sourceTreeSha256'],
                       'ruleCatalogSha256': self.static['ruleCatalog']['ruleCatalogSha256'],
                       'recognitionSha256': recognition_sha,
                       'dependencyLockSha256': self.static['executionIdentity']['dependencyLockSha256'],
                       'routeConfigurationSha256': self.static['executionIdentity']['routeConfigurationSha256'],
                       'fingerprintFileSha256': self.static['executionIdentity']['fingerprintFileSha256'],
                       'recognition': self.recognition,
                       'dexkit': self.static['executionIdentity']},
            'source': {'type': 'official',
                       'url': 'https://dldir1v6.qq.com/weixin/android/weixin8069android3040_arm64.apk'},
            'rawStageEvidence': {
                'static': {'path': 'reports/static.json', 'sha256': '2' * 64},
                'regression': {'path': 'reports/regression.json', 'sha256': '3' * 64},
                'runtime': {'path': 'reports/runtime.json', 'sha256': '4' * 64},
                'files': [{'path': 'runtime/logcat.txt', 'sha256': '5' * 64}],
            },
            'staticReport': self.static,
            'migrationBundle': {'historySha256': self.bundle.history_sha256,
                                'expectationsSha256': self.bundle.expectations_sha256,
                                'recordIds': [row['historicalRecordId'] for row in self.bundle.profiles
                                              if row['appId'] == 'wechat']},
            'acceptedAt': '2026-10-28T00:00:00+00:00',
        }
        source_cases = [{'kind': 'MIGRATION', 'recordId': row['historicalRecordId'],
                         'profileId': row['profileId'],
                         'sourceSha256': row['historicalAcceptanceSha256']}
                        for row in self.bundle.profiles if row['appId'] == 'wechat']
        self.regression_baseline = {
            'sha256': '6' * 64, 'appId': 'wechat',
            'migrationHistorySha256': self.bundle.history_sha256,
            'migrationExpectationsSha256': self.bundle.expectations_sha256,
            'v2Path': 'compatibility/wechat/acceptance-v2.json',
            'v2SnapshotSha256': None,
            'regressionRecordIds': [row['recordId'] for row in source_cases],
            'sourceCases': source_cases,
        }
        self.record['regressionBaseline'] = self.regression_baseline
        archive_root = 'compatibility/wechat/acceptance-evidence/semantic-record-1'
        source_refs = [self.record['rawStageEvidence'][name]
                       for name in ('static', 'regression', 'runtime')]
        source_refs.extend(self.record['rawStageEvidence']['files'])
        archived_names = ['static-report.json', 'regression-report.json', 'runtime-report.json',
                          'raw/raw-0000-logcat.txt']
        self.evidence_archive = {
            'path': archive_root, 'manifestPath': archive_root + '/manifest.json',
            'manifestSha256': '8' * 64,
            'files': [{'sourcePath': ref['path'], 'path': archive_root + '/' + name,
                       'sha256': ref['sha256']}
                      for ref, name in zip(source_refs, archived_names)],
        }
        self.record['rawStageEvidence'] = {
            'static': {'path': self.evidence_archive['files'][0]['path'],
                       'sha256': self.evidence_archive['files'][0]['sha256']},
            'regression': {'path': self.evidence_archive['files'][1]['path'],
                           'sha256': self.evidence_archive['files'][1]['sha256']},
            'runtime': {'path': self.evidence_archive['files'][2]['path'],
                        'sha256': self.evidence_archive['files'][2]['sha256']},
            'files': [{'path': item['path'], 'sha256': item['sha256']}
                      for item in self.evidence_archive['files'][3:]],
        }
        self.record['evidenceArchive'] = self.evidence_archive

    def test_schema2_table_preserves_complete_semantic_and_evidence_identity(self):
        table = {'schemaVersion': 2, 'appId': 'wechat', 'records': [self.record]}
        records = validate_semantic_acceptance_table(table, 'wechat', repository=self.root)
        self.assertEqual(self.record, records[0])

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
        self.assertEqual(self.record['source'], draft['source'])
        self.assertEqual(self.record['module'], draft['module'])
        self.assertEqual(self.record['rawStageEvidence'], draft['rawStageEvidence'])
        self.assertEqual(self.record['migrationBundle'], draft['migrationBundle'])

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
        mutations.append(('omitted split', omitted, 'APK input list differs'))
        changed = copy.deepcopy(self.record)
        changed['apkFiles'][1]['sha256'] = 'c' * 64
        mutations.append(('changed hash', changed, 'APK input list differs'))
        reordered = copy.deepcopy(self.record)
        reordered['apkFiles'][1], reordered['apkFiles'][2] = reordered['apkFiles'][2], reordered['apkFiles'][1]
        mutations.append(('reordered splits', reordered, 'APK input list differs'))
        duplicate_path = copy.deepcopy(self.record)
        duplicate_path['apkFiles'][2]['path'] = duplicate_path['apkFiles'][1]['path']
        mutations.append(('duplicate APK path', duplicate_path, 'duplicate APK input path'))
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

    def test_schema2_record_rejects_duplicate_static_input_paths(self):
        damaged = copy.deepcopy(self.record)
        damaged['staticReport']['inputHashes'][2]['path'] = damaged['staticReport']['inputHashes'][1]['path']
        with self.assertRaisesRegex(ValueError, 'static report contains a duplicate APK input path'):
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
        with self.assertRaisesRegex(ValueError, 'frozen migration bundle'):
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
