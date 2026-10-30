import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.ci.runtime_task import evaluate_validation
from scripts.ci.semantic_migration import load_migration_bundle
from scripts.ci.semantic_protocol import validate_check_report
from scripts.ci.semantic_regression_baseline import load_semantic_regression_baseline
from scripts.test_semantic_migration import ROOT
from scripts import test_semantic_migration


class SemanticRuntimeTaskTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        shutil.copytree(ROOT / 'compatibility', self.root / 'compatibility')
        self.bundle = load_migration_bundle(self.root)
        self.candidate, self.module, self.static, self.regression, self.smoke = self.models()

    def models(self):
        profiles = [row for row in self.bundle.profiles if row['appId'] == 'wechat']
        profile = profiles[0]
        report = test_semantic_migration.SemanticMigrationTest('runTest').report(profile)
        validate_check_report(report)
        recognition = {'appId': report['appId'],
                       'evaluatedRecognizerIds': report['evaluatedRecognizerIds'],
                       'ruleCatalogSha256': report['ruleCatalog']['ruleCatalogSha256']}
        recognition_sha = hashlib.sha256(json.dumps(
            recognition, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        candidate = {
            'schemaVersion': 2, 'appId': 'wechat', 'status': 'PREPARED',
            'identity': profile['identity'],
            'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/weixin8069android3040_arm64.apk',
            'apkFiles': [{'path': 'apk/base.apk', 'role': 'base', 'sha256': digest}
                         for digest in profile['apkInputSha256']],
            'ruleCatalog': copy.deepcopy(report['ruleCatalog']),
            'ruleCatalogSha256': report['ruleCatalog']['ruleCatalogSha256'],
            'snapshot': copy.deepcopy(report['snapshot']),
            'expectedPlan': copy.deepcopy(profile['expectedPlan']),
            'expectedExecutionSha256': profile['expectedExecutionSha256'],
            'recognition': recognition, 'recognitionSha256': recognition_sha,
            'staticReport': 'static/report.json', 'staticReportSha256': 'a' * 64,
        }
        execution = report['executionIdentity']
        module = {
            'schemaVersion': 2, 'sha256': 'c' * 64, 'sourceTreeSha256': execution['sourceTreeSha256'],
            'ruleCatalogSha256': report['ruleCatalog']['ruleCatalogSha256'],
            'recognition': recognition, 'recognitionSha256': recognition_sha,
            'routeConfigurationSha256': execution['routeConfigurationSha256'],
            'dependencyLockSha256': execution['dependencyLockSha256'],
            'fingerprintFileSha256': execution['fingerprintFileSha256'], 'dexkit': execution,
        }
        reports_dir = self.root / 'regression'
        reports_dir.mkdir()
        baseline = load_semantic_regression_baseline(self.root, 'wechat', self.bundle)
        rows = []
        for case in baseline.cases:
            if case['kind'] == 'MIGRATION':
                fixed_profile = next(row for row in profiles
                                     if row['historicalRecordId'] == case['recordId'])
                fixed_report = test_semantic_migration.SemanticMigrationTest('runTest').report(fixed_profile)
            else:
                fixed_report = copy.deepcopy(case['savedStaticReport'])
            report_path = reports_dir / f'{case["recordId"]}.json'
            report_path.write_text(json.dumps(fixed_report))
            rows.append({
                'status': 'PASSED', 'appId': fixed_profile['appId'], 'identity': fixed_profile['identity'],
                'profileId': fixed_profile['profileId'], 'recordId': fixed_profile['historicalRecordId'],
                'initialMigration': False,
                'acceptanceSnapshotSha256': fixed_profile['historicalAcceptanceSha256'],
                'migrationHistorySha256': self.bundle.history_sha256,
                'migrationExpectationsSha256': self.bundle.expectations_sha256,
                'baselineSha256': baseline.sha256,
                'v2SnapshotSha256': baseline.v2_sha256,
                'baselineSourceCase': {'kind': case['kind'], 'recordId': case['recordId'],
                                       'profileId': case['profileId'],
                                       'sourceSha256': case['acceptanceSnapshotSha256']},
                'staticEvidence': {'path': report_path.name,
                                   'sha256': hashlib.sha256(report_path.read_bytes()).hexdigest()},
            })
        result_path = reports_dir / 'results.json'
        result_path.write_text(json.dumps(rows))
        regression = {'status': 'PASSED', 'records': rows,
                      'evidence': {'path': str(result_path),
                                   'sha256': hashlib.sha256(result_path.read_bytes()).hexdigest()}}
        smoke = {
            'status': 'RUNTIME_SMOKE_VERIFIED', 'appId': 'wechat',
            'packageName': profile['identity']['packageName'], 'abi': profile['identity']['abi'],
            'apkSha256': profile['identity']['apkSha256'], 'moduleApkSha256': module['sha256'],
            'moduleSourceTreeSha256': module['sourceTreeSha256'],
            'ruleCatalogSha256': module['ruleCatalogSha256'],
            'recognitionSha256': module['recognitionSha256'],
            'dependencyLockSha256': module['dependencyLockSha256'],
            'evidence': [{'path': 'runtime/raw.json', 'sha256': 'd' * 64}],
            'evidenceReport': {'path': 'runtime/report.json', 'sha256': 'e' * 64},
        }
        return candidate, module, report, regression, smoke

    def evaluate(self, **changes):
        values = [copy.deepcopy(value) for value in
                  (self.candidate, self.module, self.static, self.regression, self.smoke)]
        for name, value in changes.items():
            values[{'candidate': 0, 'module': 1, 'static': 2, 'regression': 3, 'smoke': 4}[name]] = value
        return evaluate_validation(*values, 'success', repository=self.root,
                                   acceptance_path=self.root / 'compatibility/wechat/acceptance.json')

    def test_schema_two_complete_plan_and_pinned_regressions_can_reach_runtime_gate(self):
        result = self.evaluate()
        self.assertEqual('VERIFIED', result['outcome'])
        self.assertEqual(2, result['schemaVersion'])
        self.assertEqual(self.bundle.history_sha256, result['migrationBundle']['historySha256'])
        baseline = load_semantic_regression_baseline(self.root, 'wechat', self.bundle)
        self.assertEqual([case['recordId'] for case in baseline.cases], result['regressionRecordIds'])
        self.assertEqual(baseline.sha256, result['baselineSha256'])
        self.assertEqual(baseline.v2_sha256, result['v2SnapshotSha256'])
        self.assertNotIn('_migrationBundleObject', result)

    def test_runtime_gate_rejects_missing_case_and_tampered_baseline_binding(self):
        missing = copy.deepcopy(self.regression)
        missing['records'].pop()
        result = self.evaluate(regression=missing)
        self.assertEqual('CANDIDATE_REJECTED', result['outcome'])
        self.assertEqual('static', result['failedStage'])

        changed = copy.deepcopy(self.regression)
        changed['records'][0]['baselineSha256'] = '0' * 64
        result = self.evaluate(regression=changed)
        self.assertEqual('CANDIDATE_REJECTED', result['outcome'])
        self.assertEqual('static', result['failedStage'])

    def test_route_only_or_schema_one_candidate_cannot_be_promoted(self):
        candidate = copy.deepcopy(self.candidate)
        candidate.pop('expectedPlan')
        candidate.pop('expectedExecutionSha256')
        candidate.update(route={'appId': 'wechat', 'candidates': ['wechat_tablet_login_001']},
                         routeSha256='f' * 64)
        self.assertNotEqual('VERIFIED', self.evaluate(candidate=candidate)['outcome'])
        candidate = copy.deepcopy(self.candidate)
        candidate['schemaVersion'] = 1
        candidate.pop('expectedPlan')
        candidate.pop('expectedExecutionSha256')
        self.assertNotEqual('VERIFIED', self.evaluate(candidate=candidate)['outcome'])

    def test_fixed_report_bytes_are_reopened_and_bound_before_runtime_promotion(self):
        row = self.regression['records'][0]
        report_path = self.root / 'regression' / row['staticEvidence']['path']
        report_path.write_text('{}')
        self.assertNotEqual('VERIFIED', self.evaluate()['outcome'])

    def test_runtime_evidence_is_still_required_after_semantic_gate(self):
        smoke = copy.deepcopy(self.smoke)
        smoke['evidence'] = []
        result = self.evaluate(smoke=smoke)
        self.assertNotEqual('VERIFIED', result['outcome'])

    def test_changed_or_missing_pinned_v1_history_cannot_bootstrap_verification(self):
        path = self.root / 'compatibility/wechat/acceptance.json'
        original = path.read_bytes()
        path.write_bytes(original + b' ')
        self.assertNotEqual('VERIFIED', self.evaluate()['outcome'])
        path.unlink()
        self.assertNotEqual('VERIFIED', self.evaluate()['outcome'])


if __name__ == '__main__':
    unittest.main()
