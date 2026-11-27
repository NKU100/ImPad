import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.ci.grouped_static_regression import (group_report,
    regression_baseline_binding, regression_selection)
from scripts.ci.runtime_task import evaluate_validation
from scripts.ci.semantic_regression_baseline import load_verified_regression_pool

ROOT = Path(__file__).resolve().parents[1]


class SemanticRuntimeTaskTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        compatibility = self.root / 'compatibility'
        shutil.copytree(ROOT / 'compatibility', compatibility)
        self.pool = load_verified_regression_pool(self.root, 'wechat')
        self.selection = regression_selection(self.pool)
        self.group = self.selection['selectedGroups'][0]
        self.case = self.group['cases'][0]
        table = json.loads((compatibility / 'wechat/acceptance-v2.json').read_text())
        self.record = next(row for row in table['records'] if row['recordId'] == self.case['recordId'])
        self.candidate, self.module, self.static = self.models()
        self.regression, self.static_evidence = self.grouped_regression()
        self.smoke = self.runtime_report()

    def models(self):
        static = copy.deepcopy(self.record['staticReport'])
        recognition = copy.deepcopy(self.record['recognition'])
        candidate = {
            'schemaVersion': 2, 'appId': 'wechat', 'status': 'PREPARED',
            'identity': copy.deepcopy(self.record['identity']),
            'sourceUrl': self.record['source']['url'],
            'apkFiles': [{'path': f'apk/{item["sha256"]}.apk', **item}
                         for item in self.record['apkFiles']],
            'ruleCatalog': copy.deepcopy(static['ruleCatalog']),
            'ruleCatalogSha256': static['ruleCatalog']['ruleCatalogSha256'],
            'snapshot': copy.deepcopy(static['snapshot']),
            'expectedPlan': copy.deepcopy(self.record['completePlan']),
            'expectedExecutionSha256': self.record['executionSha256'],
            'recognition': recognition,
            'recognitionSha256': self.record['recognitionSha256'],
            'staticReport': 'static/report.json', 'staticReportSha256': 'a' * 64,
        }
        return candidate, copy.deepcopy(self.record['module']), static

    def grouped_regression(self, passed=False):
        rows = []
        static_evidence = None
        for index, group in enumerate(self.selection['selectedGroups']):
            ids = [case['recordId'] for case in group['cases']]
            if passed and index == 0:
                selected_id = ids[0]
                report = copy.deepcopy(self.case['savedStaticReport'])
                static_evidence = self.root / 'regression' / 'sample-static.json'
                static_evidence.parent.mkdir(parents=True, exist_ok=True)
                static_evidence.write_text(json.dumps(report))
                rows.append({'groupId': group['groupId'], 'candidateRecordIds': ids,
                             'cacheMissRecordIds': [], 'unavailable': [],
                             'status': 'PASSED', 'recordId': selected_id,
                             'apkSource': 'cache',
                             'staticEvidence': {'path': static_evidence.name,
                                                'sha256': hashlib.sha256(static_evidence.read_bytes()).hexdigest()}})
            else:
                rows.append({'groupId': group['groupId'], 'candidateRecordIds': ids,
                             'cacheMissRecordIds': ids,
                             'unavailable': [{'recordId': value, 'error': 'HTTP 404'} for value in ids],
                             'status': 'SKIPPED', 'skipReason': 'APK_UNAVAILABLE'})
        report = group_report('wechat', self.pool.v2_sha256, self.selection, rows)
        path = self.root / 'regression' / 'results.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report))
        wrapper = dict(report, status=('PARTIAL' if any(row['status'] == 'SKIPPED' for row in rows)
                                       else 'PASSED'), evidence={
            'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        return wrapper, static_evidence

    def runtime_report(self):
        return {
            'status': 'RUNTIME_SMOKE_VERIFIED', 'appId': 'wechat',
            'packageName': self.candidate['identity']['packageName'],
            'abi': self.candidate['identity']['abi'],
            'apkSha256': self.candidate['identity']['apkSha256'],
            'moduleApkSha256': self.module['sha256'],
            'moduleSourceTreeSha256': self.module['sourceTreeSha256'],
            'ruleCatalogSha256': self.module['ruleCatalogSha256'],
            'recognitionSha256': self.module['recognitionSha256'],
            'dependencyLockSha256': self.module['dependencyLockSha256'],
            'evidence': [{'path': 'raw/runtime.log', 'sha256': 'd' * 64}],
            'evidenceReport': {'path': 'runtime/report.json', 'sha256': 'e' * 64},
        }

    def evaluate(self, **changes):
        values = [copy.deepcopy(value) for value in
                  (self.candidate, self.module, self.static, self.regression, self.smoke)]
        for name, value in changes.items():
            values[{'candidate': 0, 'module': 1, 'static': 2, 'regression': 3, 'smoke': 4}[name]] = value
        return evaluate_validation(*values, 'success', repository=self.root,
            acceptance_path=self.root / 'compatibility/wechat/acceptance-v2.json')

    def test_grouped_v2_baseline_and_runtime_evidence_can_reach_verification(self):
        result = self.evaluate()
        self.assertEqual('VERIFIED', result['outcome'])
        self.assertEqual(2, result['schemaVersion'])
        self.assertEqual(regression_baseline_binding(self.pool), result['regressionBaseline'])
        self.assertEqual([row['groupId'] for row in self.regression['groups']],
                         result['regressionRecordIds'])
        self.assertNotIn('migrationBundle', result)
        self.assertNotIn('baselineSha256', result)

    def test_runtime_gate_rejects_missing_or_changed_group_evidence(self):
        missing = copy.deepcopy(self.regression)
        missing['groups'].pop()
        result = self.evaluate(regression=missing)
        self.assertEqual('CANDIDATE_REJECTED', result['outcome'])
        changed = copy.deepcopy(self.regression)
        changed['acceptanceSha256'] = '0' * 64
        result = self.evaluate(regression=changed)
        self.assertEqual('CANDIDATE_REJECTED', result['outcome'])

    def test_passed_group_static_report_is_reopened_and_execution_bound(self):
        regression, report_path = self.grouped_regression(passed=True)
        self.assertIsNotNone(report_path)
        result = self.evaluate(regression=regression)
        self.assertEqual('VERIFIED', result['outcome'])
        report_path.write_text(report_path.read_text() + ' ')
        result = self.evaluate(regression=regression)
        self.assertEqual('CANDIDATE_REJECTED', result['outcome'])

    def test_route_only_candidate_and_missing_runtime_evidence_cannot_be_promoted(self):
        candidate = copy.deepcopy(self.candidate)
        candidate.pop('expectedPlan')
        candidate.pop('expectedExecutionSha256')
        candidate['schemaVersion'] = 1
        self.assertNotEqual('VERIFIED', self.evaluate(candidate=candidate)['outcome'])
        smoke = copy.deepcopy(self.smoke)
        smoke['evidence'] = []
        self.assertNotEqual('VERIFIED', self.evaluate(smoke=smoke)['outcome'])

    def test_cancelled_and_unavailable_runtime_still_block_verification(self):
        cancelled = evaluate_validation(self.candidate, self.module, self.static,
            self.regression, self.smoke, 'cancelled', repository=self.root,
            acceptance_path=self.root / 'compatibility/wechat/acceptance-v2.json')
        self.assertEqual('CANCELLED', cancelled['outcome'])
        unavailable = copy.deepcopy(self.smoke)
        unavailable['status'] = 'ACCEPTANCE_UNAVAILABLE'
        result = self.evaluate(smoke=unavailable)
        self.assertEqual('UNAVAILABLE', result['outcome'])


if __name__ == '__main__':
    unittest.main()
