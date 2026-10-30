import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.ci.semantic_migration import load_migration_bundle
from scripts.ci.semantic_validation import validate_semantic_stage_models
from scripts.test_semantic_migration import ROOT, SemanticMigrationTest as _SemanticMigrationTest


class SemanticValidationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / 'compatibility', self.root / 'compatibility')
        self.bundle = load_migration_bundle(self.root)

    @staticmethod
    def report(profile):
        return _SemanticMigrationTest.report(_SemanticMigrationTest('runTest'), profile)

    def models(self, app_id='wechat'):
        profile = next(row for row in self.bundle.profiles if row['appId'] == app_id)
        report = self.report(profile)
        from scripts.ci.semantic_protocol import validate_check_report
        validate_check_report(report)
        recognition = {'appId': report['appId'],
                       'evaluatedRecognizerIds': report['evaluatedRecognizerIds'],
                       'ruleCatalogSha256': report['ruleCatalog']['ruleCatalogSha256']}
        import hashlib
        import json
        recognition_sha = hashlib.sha256(json.dumps(recognition, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        static = copy.deepcopy(report)
        candidate = {'schemaVersion': 2, 'appId': app_id, 'identity': profile['identity'],
                     'ruleCatalogSha256': report['ruleCatalog']['ruleCatalogSha256'],
                     'ruleCatalog': copy.deepcopy(report['ruleCatalog']),
                     'snapshot': copy.deepcopy(report['snapshot']),
                     'apkFiles': [{'sha256': row['sha256']} for row in report['inputHashes']],
                     'expectedPlan': profile['expectedPlan'],
                     'expectedExecutionSha256': profile['expectedExecutionSha256'],
                     'recognition': recognition, 'recognitionSha256': recognition_sha,
                     'staticReport': 'static/report.json', 'staticReportSha256': 'a' * 64}
        execution = report['executionIdentity']
        module = {'schemaVersion': 2, 'ruleCatalogSha256': report['ruleCatalog']['ruleCatalogSha256'],
                  'dexkit': execution, 'recognition': recognition, 'recognitionSha256': recognition_sha}
        for key in ('sourceTreeSha256', 'dependencyLockSha256', 'routeConfigurationSha256', 'fingerprintFileSha256'):
            module[key] = execution[key]
        profiles = [row for row in self.bundle.profiles if row['appId'] == app_id]
        from scripts.ci.semantic_regression_baseline import load_semantic_regression_baseline
        baseline = load_semantic_regression_baseline(self.root, app_id, self.bundle)
        rows = [{'status': 'PASSED', 'appId': row['appId'], 'identity': row['identity'],
                 'profileId': row['profileId'], 'recordId': row['historicalRecordId'],
                 'initialMigration': False, 'acceptanceSnapshotSha256': row['historicalAcceptanceSha256'],
                 'migrationHistorySha256': self.bundle.history_sha256,
                 'migrationExpectationsSha256': self.bundle.expectations_sha256,
                 'baselineSha256': baseline.sha256,
                 'v2SnapshotSha256': baseline.v2_sha256,
                 'baselineSourceCase': {'kind': 'MIGRATION', 'recordId': row['historicalRecordId'],
                                        'profileId': row['profileId'],
                                        'sourceSha256': row['historicalAcceptanceSha256']},
                 'staticEvidence': {'path': f'reports/{row["profileId"]}.json', 'sha256': 'b' * 64}}
                for row in profiles]
        reports = {row['historicalRecordId']: self.report(row) for row in profiles}
        return candidate, module, static, rows, reports

    def test_accepts_each_app_fixed_regression_profiles_and_returns_bounded_status(self):
        for app_id in ('wechat', 'qq', 'wecom'):
            with self.subTest(app_id=app_id):
                candidate, module, static, rows, reports = self.models(app_id)
                result = validate_semantic_stage_models(candidate, module, static, rows, self.bundle,
                                                        reports, repository=self.root)
                self.assertEqual(2, result['schemaVersion'])
                self.assertEqual('PASSED', result['staticStatus'])
                self.assertEqual('PASSED', result['regressionStatus'])
                self.assertNotIn('runtimeStatus', result)

    def test_rejects_changed_candidate_static_selection_and_module_identity(self):
        mutations = (
            lambda c, m, s: c.update(expectedPlan={}),
            lambda c, m, s: c.update(identity={}),
            lambda c, m, s: c.update(ruleCatalog={}),
            lambda c, m, s: c.update(snapshot={}),
            lambda c, m, s: s['selection'].update(status='NO_MATCH'),
            lambda c, m, s: m.update(sourceTreeSha256='0' * 64),
            lambda c, m, s: m.update(dexkit={**m['dexkit'], 'routeConfigurationSha256': '0' * 64}),
            lambda c, m, s: m.update(fingerprintFileSha256='0' * 64),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                candidate, module, static, rows, reports = self.models()
                mutate(candidate, module, static)
                with self.assertRaises(ValueError):
                    validate_semantic_stage_models(candidate, module, static, rows, self.bundle, reports,
                                                   repository=self.root)

    def test_rejects_stale_missing_duplicate_or_faked_regression_evidence(self):
        mutations = (
            lambda rows, reports: rows[0].update(migrationHistorySha256='0' * 64),
            lambda rows, reports: rows.pop(),
            lambda rows, reports: rows.append(copy.deepcopy(rows[0])),
            lambda rows, reports: reports.pop(next(iter(reports))),
            lambda rows, reports: reports.update(unknown={}),
            lambda rows, reports: reports.__setitem__(next(iter(reports)), {'schemaVersion': 2, 'status': 'VERIFIED'}),
            lambda rows, reports: rows[0].update(recordId='unknown'),
            lambda rows, reports: rows[0].update(status='FAILED'),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                candidate, module, static, rows, reports = self.models()
                mutate(rows, reports)
                with self.assertRaises(ValueError):
                    validate_semantic_stage_models(candidate, module, static, rows, self.bundle, reports,
                                                   repository=self.root)

    def test_rejects_self_consistent_fixed_report_from_different_execution_identity(self):
        candidate, module, static, rows, reports = self.models()
        report = next(iter(reports.values()))
        report['executionIdentity']['sourceTreeSha256'] = 'c' * 64
        from scripts.ci.semantic_protocol import execution_identity_sha256
        report['executionIdentity']['cacheIdentitySha256'] = execution_identity_sha256(report['executionIdentity'])
        validate = __import__('scripts.ci.semantic_protocol', fromlist=['validate_check_report']).validate_check_report
        validate(report)
        with self.assertRaises(ValueError):
            validate_semantic_stage_models(candidate, module, static, rows, self.bundle, reports,
                                           repository=self.root)

    def test_rejects_missing_or_unsafe_regression_static_evidence_reference(self):
        for reference in (None, {'path': '../escape.json', 'sha256': 'b' * 64},
                          {'path': '/outside/report.json', 'sha256': 'b' * 64},
                          {'path': 'reports/report.json', 'sha256': 'bad'},
                          {'path': 'reports/report.json', 'sha256': 'b' * 64, 'extra': True}):
            candidate, module, static, rows, reports = self.models()
            rows[0]['staticEvidence'] = reference
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                validate_semantic_stage_models(candidate, module, static, rows, self.bundle, reports,
                                               repository=self.root)

    def test_rejects_unsafe_static_reference_and_invalid_digest(self):
        for reference in ('../escape.json', '/absolute/report.json', 'static/report.json'):
            candidate, module, static, rows, reports = self.models()
            candidate['staticReport'] = reference
            if reference == 'static/report.json':
                candidate['staticReportSha256'] = 'bad'
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                validate_semantic_stage_models(candidate, module, static, rows, self.bundle, reports,
                                               repository=self.root)


if __name__ == '__main__':
    unittest.main()
