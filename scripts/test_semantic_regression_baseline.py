import copy
import hashlib
import importlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.ci.semantic_migration import load_migration_bundle
from scripts.test_semantic_migration import ROOT as REPOSITORY_ROOT
from scripts.test_semantic_protocol import check_report


class SemanticRegressionBaselineTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / 'compatibility', self.root / 'compatibility')
        self.bundle = load_migration_bundle(self.root)

    def api(self):
        spec = importlib.util.find_spec('scripts.ci.semantic_regression_baseline')
        self.assertIsNotNone(spec, 'semantic regression baseline module is missing')
        return importlib.import_module('scripts.ci.semantic_regression_baseline')

    def static_report(self, profile, apk_hashes=None):
        from scripts.ci.semantic_protocol import execution_identity_sha256, plan_execution_sha256, snapshot_sha256
        value = check_report()
        identity = copy.deepcopy(profile['identity'])
        input_hashes = list(profile['apkInputSha256'])
        if apk_hashes is not None:
            input_hashes = list(apk_hashes)
            identity['apkSha256'] = input_hashes[0]
        snapshot = copy.deepcopy(profile['snapshot'])
        snapshot['archives'][0]['containerSha256'] = input_hashes[0]
        for index, digest in enumerate(input_hashes[1:], start=1):
            dex_sha = hashlib.sha256(f'test-split-dex-{digest}'.encode()).hexdigest()
            snapshot['loaders'][0]['dexSha256'].append(dex_sha)
            snapshot['archives'].append({
                'loaderId': snapshot['readyLoaderId'], 'containerIndex': index,
                'kind': 'ZIP', 'containerSha256': digest, 'dexSha256': [dex_sha],
            })
        snapshot['contentSha256'] = snapshot_sha256(snapshot)
        catalog = copy.deepcopy(profile['ruleCatalog'])
        plan = copy.deepcopy(profile['expectedPlan'])
        plan['snapshotSha256'] = snapshot['contentSha256']
        execution_sha = plan_execution_sha256(plan)
        recognizer_ids = list(catalog['recognizerIds'])
        recognizer = recognizer_ids[0]
        value.update(appId=profile['appId'], identity=identity,
                     inputHashes=[{'path': f'input-{index}.apk', 'sha256': digest}
                                  for index, digest in enumerate(input_hashes)],
                     ruleCatalog=catalog, snapshot=snapshot,
                     evaluatedRecognizerIds=recognizer_ids,
                     recognizers=[{'recognizerId': recognizer, 'status': 'PLANS',
                                   'plans': [{'plan': plan, 'executionSha256': execution_sha,
                                              'evidence': ['fixed semantic test fixture']}],
                                   'diagnostics': [], 'reason': None}])
        value['selection'].update(status='SELECTED', plan=plan, executionSha256=execution_sha,
                                  evidenceByRecognizer={recognizer: ['fixed semantic test fixture']})
        value['executionIdentity']['ruleCatalogSha256'] = catalog['ruleCatalogSha256']
        value['executionIdentity']['cacheIdentitySha256'] = execution_identity_sha256(value['executionIdentity'])
        return value

    def accepted_record(self, profile, record_id='semantic-v2-test', apk_hashes=None):
        record = self.matching_migration_record(profile, record_id)
        static = self.static_report(profile, apk_hashes)
        record['identity'] = copy.deepcopy(static['identity'])
        record['apkFiles'] = [
            {'path': f'apk-inputs/input-{index}.apk',
             'role': 'base' if index == 0 else 'split', 'sha256': row['sha256']}
            for index, row in enumerate(static['inputHashes'])
        ]
        record['staticReport'] = static
        record['completePlan'] = copy.deepcopy(static['selection']['plan'])
        record['executionSha256'] = static['selection']['executionSha256']
        from scripts.ci.semantic_validation import _recognition
        record['recognition'], record['recognitionSha256'] = _recognition(static)
        record['module']['recognition'] = copy.deepcopy(record['recognition'])
        record['module']['recognitionSha256'] = record['recognitionSha256']
        record['module']['dexkit'] = copy.deepcopy(static['executionIdentity'])
        for key in ('sourceTreeSha256', 'dependencyLockSha256',
                    'routeConfigurationSha256', 'fingerprintFileSha256'):
            record['module'][key] = static['executionIdentity'][key]
        return record

    def write_table(self, records):
        path = self.root / 'compatibility/wechat/acceptance-v2.json'
        path.write_text(json.dumps({'schemaVersion': 2, 'appId': 'wechat', 'records': records}, indent=2) + '\n')
        return path

    def matching_migration_record(self, profile, record_id='semantic-v2-reacceptance'):
        from scripts.test_semantic_acceptance import SemanticAcceptanceTest
        fixture = SemanticAcceptanceTest('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        record = copy.deepcopy(fixture.record)
        static = self.static_report(profile, profile['apkInputSha256'])
        record['identity'] = copy.deepcopy(profile['identity'])
        record['apkFiles'] = [
            {'path': f'apk-inputs/frozen-{index}.apk',
             'role': 'base' if index == 0 else 'split', 'sha256': digest}
            for index, digest in enumerate(profile['apkInputSha256'])
        ]
        record['staticReport'] = static
        record['completePlan'] = copy.deepcopy(static['selection']['plan'])
        record['executionSha256'] = static['selection']['executionSha256']
        from scripts.ci.semantic_validation import _recognition
        record['recognition'], record['recognitionSha256'] = _recognition(static)
        record['module']['recognition'] = copy.deepcopy(record['recognition'])
        record['module']['recognitionSha256'] = record['recognitionSha256']
        record['module']['dexkit'] = copy.deepcopy(static['executionIdentity'])
        for key in ('sourceTreeSha256', 'dependencyLockSha256',
                    'routeConfigurationSha256', 'fingerprintFileSha256'):
            record['module'][key] = static['executionIdentity'][key]
        fixture._change_record_id(record, record_id)
        return record

    def test_load_preserves_frozen_profiles_and_adds_strict_v2_cases(self):
        baseline_api = self.api()
        pinned = {name: (self.root / name).read_bytes() for name in (
            'compatibility/wechat/acceptance.json',
            'compatibility/semantic-migration/history.json',
            'compatibility/semantic-migration/expectations.json',
        )}
        frozen = baseline_api.load_semantic_regression_baseline(self.root, 'wechat', self.bundle)
        self.assertEqual(2, len(frozen.cases))
        self.assertTrue(all(case['kind'] == 'MIGRATION' for case in frozen.cases))
        self.assertIsNone(frozen.v2_raw_bytes)
        self.assertIsNone(frozen.v2_sha256)
        for case in frozen.cases:
            profile = next(p for p in self.bundle.profiles if p['historicalRecordId'] == case['recordId'])
            baseline_api.compare_baseline_report(frozen, case['recordId'], self.static_report(profile))

        record = self.accepted_record(self.bundle.profiles[0], apk_hashes=['c' * 64, 'd' * 64, 'e' * 64])
        raw_path = self.write_table([record])
        raw = raw_path.read_bytes()
        combined = baseline_api.load_semantic_regression_baseline(self.root, 'wechat', self.bundle)
        self.assertEqual(3, len(combined.cases))
        accepted = combined.cases[-1]
        self.assertEqual('ACCEPTED_V2', accepted['kind'])
        self.assertEqual(record['recordId'], accepted['recordId'])
        self.assertEqual(record['source'], accepted['source'])
        self.assertEqual(record['staticReport'], accepted['savedStaticReport'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), combined.v2_sha256)
        self.assertEqual(raw, combined.v2_raw_bytes)
        self.assertEqual(pinned, {name: (self.root / name).read_bytes() for name in pinned})

    def test_v2_report_comparison_pins_complete_plan_execution_snapshot_identity_and_ordered_hashes(self):
        baseline_api = self.api()
        profile = self.bundle.profiles[0]
        record = self.accepted_record(profile, apk_hashes=['c' * 64, 'd' * 64, 'e' * 64])
        self.write_table([record])
        baseline = baseline_api.load_semantic_regression_baseline(self.root, 'wechat')
        report = copy.deepcopy(record['staticReport'])
        for index, row in enumerate(report['inputHashes']):
            row['path'] = f'/relocated/output-{index}.apk'
        baseline_api.compare_baseline_report(baseline, record['recordId'], report)

        mutations = (
            lambda value: value['identity'].update(versionCode=value['identity']['versionCode'] + 1),
            lambda value: value['selection']['plan']['hooks'][0].update(descriptor='Lchanged;->x()V'),
            lambda value: value['selection'].update(executionSha256='f' * 64),
            lambda value: value['snapshot'].update(contentSha256='f' * 64),
            lambda value: value['inputHashes'].__setitem__(slice(None), list(reversed(value['inputHashes']))),
        )
        for mutate in mutations:
            changed = copy.deepcopy(record['staticReport'])
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                baseline_api.compare_baseline_report(baseline, record['recordId'], changed)

    def test_baseline_rejects_duplicate_record_id_and_frozen_input_identity(self):
        baseline_api = self.api()
        profile = self.bundle.profiles[0]
        duplicate_id = self.matching_migration_record(profile, profile['historicalRecordId'])
        self.write_table([duplicate_id])
        with self.assertRaisesRegex(ValueError, 'recordId'):
            baseline_api.load_semantic_regression_baseline(self.root, 'wechat', self.bundle)

        matching_input = self.matching_migration_record(profile)
        self.write_table([matching_input])
        baseline = baseline_api.load_semantic_regression_baseline(self.root, 'wechat', self.bundle)
        migration_ids = [row['historicalRecordId'] for row in self.bundle.profiles
                         if row['appId'] == 'wechat']
        self.assertEqual([*migration_ids, matching_input['recordId']],
                         [case['recordId'] for case in baseline.cases])
        self.assertEqual(['MIGRATION', 'MIGRATION', 'ACCEPTED_V2'],
                         [case['kind'] for case in baseline.cases])
        for case in baseline.cases:
            report = (case['savedStaticReport'] if case['kind'] == 'ACCEPTED_V2'
                      else self.static_report(next(row for row in self.bundle.profiles
                                                   if row['historicalRecordId'] == case['recordId'])))
            baseline_api.compare_baseline_report(baseline, case['recordId'], report)

    def test_same_input_with_conflicting_plan_is_rejected(self):
        baseline_api = self.api()
        profile = self.bundle.profiles[0]
        conflicting = self.matching_migration_record(profile, 'semantic-v2-conflicting-plan')
        altered = copy.deepcopy(conflicting['completePlan'])
        hook = altered['hooks'][0]
        replacement_index = next(index for index, digest in enumerate(
            conflicting['staticReport']['snapshot']['loaders'][0]['dexSha256'])
            if index != hook['source']['dexIndex'])
        hook['source']['dexIndex'] = replacement_index
        hook['source']['dexSha256'] = conflicting['staticReport']['snapshot']['loaders'][0]['dexSha256'][replacement_index]
        from scripts.ci.semantic_protocol import plan_execution_sha256
        execution_sha = plan_execution_sha256(altered)
        conflicting['completePlan'] = altered
        conflicting['executionSha256'] = execution_sha
        conflicting['staticReport']['selection']['plan'] = copy.deepcopy(altered)
        conflicting['staticReport']['selection']['executionSha256'] = execution_sha
        for recognizer in conflicting['staticReport']['recognizers']:
            for envelope in recognizer['plans']:
                envelope['plan'] = copy.deepcopy(altered)
                envelope['executionSha256'] = execution_sha
        self.write_table([conflicting])
        with self.assertRaisesRegex(ValueError, 'conflicting expectations'):
            baseline_api.load_semantic_regression_baseline(self.root, 'wechat', self.bundle)

    def test_assert_unchanged_detects_v2_table_add_change_and_delete(self):
        baseline_api = self.api()
        profile = self.bundle.profiles[0]
        first = self.accepted_record(profile, 'semantic-v2-first', ['c' * 64, 'd' * 64, 'e' * 64])
        second = self.accepted_record(profile, 'semantic-v2-second', ['f' * 64, 'a' * 64, 'b' * 64])
        path = self.write_table([first])
        baseline = baseline_api.load_semantic_regression_baseline(self.root, 'wechat')
        changed = json.loads(path.read_text())
        changed['records'][0]['acceptedAt'] = '2026-10-28T02:00:00+00:00'
        path.write_text(json.dumps(changed, indent=2) + '\n')
        with self.assertRaisesRegex(ValueError, 'changed'):
            baseline.assert_unchanged()

        path.write_text(json.dumps({'schemaVersion': 2, 'appId': 'wechat', 'records': [first]}, indent=2) + '\n')
        baseline = baseline_api.load_semantic_regression_baseline(self.root, 'wechat')
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'changed'):
            baseline.assert_unchanged()

        self.write_table([first])
        baseline = baseline_api.load_semantic_regression_baseline(self.root, 'wechat')
        self.write_table([first, second])
        with self.assertRaisesRegex(ValueError, 'changed'):
            baseline.assert_unchanged()

    def test_baseline_rejects_malformed_v2_table_and_mutated_frozen_bundle(self):
        baseline_api = self.api()
        path = self.root / 'compatibility/wechat/acceptance-v2.json'
        path.write_bytes(b'{not json')
        with self.assertRaises(ValueError):
            baseline_api.load_semantic_regression_baseline(self.root, 'wechat', self.bundle)
        path.unlink()

        baseline = baseline_api.load_semantic_regression_baseline(self.root, 'wechat', self.bundle)
        history = self.root / 'compatibility/semantic-migration/history.json'
        history.write_bytes(history.read_bytes() + b'\n')
        with self.assertRaises(ValueError):
            baseline.assert_unchanged()

    def test_malformed_or_self_consistent_but_wrong_v2_static_expectation_is_rejected(self):
        baseline_api = self.api()
        profile = self.bundle.profiles[0]
        malformed = self.accepted_record(profile, 'malformed-static', ['c' * 64, 'd' * 64, 'e' * 64])
        malformed['staticReport']['snapshot']['contentSha256'] = 'f' * 64
        self.write_table([malformed])
        with self.assertRaises(ValueError):
            baseline_api.load_semantic_regression_baseline(self.root, 'wechat')

        changed = self.accepted_record(profile, 'wrong-plan', ['f' * 64, 'a' * 64, 'b' * 64])
        changed['staticReport']['selection']['plan']['hooks'][0]['descriptor'] = 'Lchanged;->x()V'
        self.write_table([changed])
        with self.assertRaises(ValueError):
            baseline_api.load_semantic_regression_baseline(self.root, 'wechat')


if __name__ == '__main__':
    unittest.main()
