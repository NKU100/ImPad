import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.ci.grouped_static_regression import group_report, regression_selection
from scripts.ci.semantic_regression_baseline import load_verified_regression_pool
from scripts.ci.semantic_validation import validate_semantic_stage_models

ROOT = Path(__file__).resolve().parents[1]


class SemanticValidationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def models(self, app_id='wechat'):
        compatibility = self.root / 'compatibility' / app_id
        compatibility.parent.mkdir(parents=True, exist_ok=True)
        if not compatibility.exists():
            shutil.copytree(ROOT / 'compatibility' / app_id, compatibility)
        pool = load_verified_regression_pool(self.root, app_id)
        table = json.loads((compatibility / 'acceptance-v2.json').read_text())
        record = next(item for item in table['records'] if item['recordId'] == pool.cases[0]['recordId'])
        static = copy.deepcopy(record['staticReport'])
        candidate = {
            'schemaVersion': 2,
            'appId': app_id,
            'identity': copy.deepcopy(record['identity']),
            'ruleCatalogSha256': static['ruleCatalog']['ruleCatalogSha256'],
            'ruleCatalog': copy.deepcopy(static['ruleCatalog']),
            'snapshot': copy.deepcopy(static['snapshot']),
            'apkFiles': [{'sha256': item['sha256']} for item in record['apkFiles']],
            'expectedPlan': copy.deepcopy(record['completePlan']),
            'expectedExecutionSha256': record['executionSha256'],
            'recognition': copy.deepcopy(record['recognition']),
            'recognitionSha256': record['recognitionSha256'],
            'staticReport': 'static/report.json',
            'staticReportSha256': 'a' * 64,
        }
        module = copy.deepcopy(record['module'])
        return candidate, module, static, pool

    def grouped_report(self, pool):
        selection = regression_selection(pool)
        rows = []
        for group in selection['selectedGroups']:
            ids = [case['recordId'] for case in group['cases']]
            rows.append({'groupId': group['groupId'], 'candidateRecordIds': ids,
                         'cacheMissRecordIds': ids,
                         'unavailable': [{'recordId': record_id, 'error': 'HTTP 404'}
                                         for record_id in ids],
                         'status': 'SKIPPED', 'skipReason': 'APK_UNAVAILABLE'})
        return group_report(pool.app_id, pool.v2_sha256, selection, rows)

    def validate(self, candidate, module, static, pool, report):
        path = self.root / 'regression' / 'results.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report))
        return validate_semantic_stage_models(
            candidate, module, static, report, repository=self.root,
            baseline=pool, grouped_report_path=path)

    def test_accepts_grouped_v2_regression_for_each_supported_app(self):
        for app_id in ('wechat', 'qq', 'wecom'):
            with self.subTest(app_id=app_id):
                candidate, module, static, pool = self.models(app_id)
                result = self.validate(candidate, module, static, pool, self.grouped_report(pool))
                self.assertEqual('PASSED', result['staticStatus'])
                self.assertEqual('PARTIAL', result['regressionStatus'])
                self.assertEqual(pool.v2_sha256, result['regressionBaseline']['v2SnapshotSha256'])
                self.assertEqual(app_id, result['regressionBaseline']['appId'])
                self.assertEqual(regression_selection(pool)['version'],
                                 result['regressionBaseline']['groupingVersion'])
                self.assertNotIn('migrationBundle', result)
                self.assertNotIn('migrationHistorySha256', result)
                self.assertNotIn('sourceCases', result['regressionBaseline'])

    def test_rejects_grouped_rows_that_do_not_bind_current_group_selection(self):
        candidate, module, static, pool = self.models()
        report = self.grouped_report(pool)
        report['groups'][0]['candidateRecordIds'] = ['unknown-record']
        with self.assertRaisesRegex(ValueError, 'selected group'):
            self.validate(candidate, module, static, pool, report)

    def test_rejects_candidate_plan_and_module_identity_mismatches(self):
        report = None
        for change in ('plan', 'module'):
            with self.subTest(change=change):
                candidate, module, static, pool = self.models()
                report = self.grouped_report(pool)
                if change == 'plan':
                    candidate['expectedPlan'] = {}
                else:
                    module['sourceTreeSha256'] = '0' * 64
                with self.assertRaises(ValueError):
                    self.validate(candidate, module, static, pool, report)


if __name__ == '__main__':
    unittest.main()
