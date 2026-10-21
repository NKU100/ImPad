import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch

from scripts.ci import acceptance
from scripts.ci.acceptance import AcceptanceSnapshot, load_records, make_record, select_representatives
from scripts.ci.app_policy import get_policy
from scripts.ci.runtime_task import evaluate_validation, verify_cli


class AcceptanceTest(unittest.TestCase):
    def test_only_explicit_initial_migration_accepts_missing_empty_table(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'acceptance.json'
            self.assertEqual([], load_records(path, 'qq', initial_migration=True))
            with self.assertRaises(FileNotFoundError):
                load_records(path, 'qq', initial_migration=False)

    def test_verify_cli_never_exposes_initial_migration_for_any_application(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for app_id in ('wechat', 'qq', 'wecom'):
                arguments = [
                    '--app-id', app_id, '--candidate', str(root / 'candidate.json'),
                    '--module-apk', str(root / 'module.apk'), '--regression', str(root / 'regression.json'),
                    '--smoke', str(root / 'smoke.json'), '--evidence-root', str(root),
                    '--acceptance', str(root / 'acceptance.json'), '--repository', str(root),
                    '--conclusion', 'success', '--initial-migration', '--output', str(root / 'result.json'),
                ]
                with self.subTest(app_id=app_id), \
                     patch('scripts.ci.runtime_task.verify_validation_files') as verifier, \
                     redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    verify_cli(arguments)
                verifier.assert_not_called()
            self.assertFalse((root / 'result.json').exists())

    def test_existing_damaged_or_wrong_app_table_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'acceptance.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                load_records(path, 'qq', initial_migration=True)

    def test_existing_empty_table_is_only_valid_for_initial_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'acceptance.json'
            path.write_text(json.dumps({'schemaVersion': 1, 'appId': 'qq', 'records': []}))
            self.assertEqual([], load_records(path, 'qq', initial_migration=True))
            with self.assertRaisesRegex(ValueError, 'unexpectedly empty'):
                load_records(path, 'qq', initial_migration=False)
            path.write_text(json.dumps({'schemaVersion': 1, 'appId': 'wecom', 'records': []}))
            with self.assertRaises(ValueError):
                load_records(path, 'qq', initial_migration=True)

    def test_existing_record_with_incomplete_descriptor_shape_is_rejected(self):
        identity = {'packageName': 'com.tencent.mobileqq', 'versionName': '9.3.1',
                    'versionCode': 14378, 'abi': 'arm64-v8a', 'apkSha256': 'a' * 64,
                    'signerSha256': get_policy('qq').signer_sha256}
        record = {'recordId': 'record-1', 'appId': 'qq', 'status': 'VERIFIED',
                  'identity': identity,
                  'expectedPlan': {'selection': 'UNIQUE', 'ruleId': 'qq_device_type_001',
                                   'strategyId': 'qq_tablet_001', 'targets': {'device_type': {
                                       'descriptor': 'Lx/Y;->a()V', 'shape': {}}}},
                  'source': {'type': 'official',
                             'url': 'https://downv6.qq.com/qqweb/QQ_1/android_apk/9.3.1.apk',
                             'apkFiles': [{'path': 'apk-inputs/base.apk', 'role': 'base', 'sha256': 'a' * 64}]},
                  'ruleOrder': 1, 'firstForRule': True, 'firstRecordId': 'record-1'}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'acceptance.json'
            path.write_text(json.dumps({'schemaVersion': 1, 'appId': 'qq', 'records': [record]}))
            with self.assertRaisesRegex(ValueError, 'plan|descriptor|shape|catalog'):
                load_records(path, 'qq', initial_migration=False)

    def test_expected_plan_rejects_rule_outside_candidate_version_binding(self):
        candidate, _, static, _, _ = self.fixture()
        static['ruleCatalog']['bindings'][0]['min'] = 14379
        with self.assertRaisesRegex(ValueError, 'version binding'):
            acceptance.validate_expected_plan(candidate['expectedPlan'], static['ruleCatalog'], 14378)

    def test_historical_record_catalog_digest_remains_bound_to_its_tested_module(self):
        candidate, module, static, _, _ = self.fixture()
        old_catalog_sha = 'c' * 64
        module['ruleCatalogSha256'] = old_catalog_sha
        record = {
            'recordId': 'historical-record', 'schemaVersion': 1, 'appId': 'qq',
            'status': 'VERIFIED', 'identity': candidate['identity'],
            'source': {'type': 'official', 'url': candidate['sourceUrl'],
                       'apkFiles': candidate['apkFiles']},
            'expectedPlan': candidate['expectedPlan'], 'route': {'appId': 'qq', 'candidates': ['qq_device_type_001']},
            'module': module, 'ruleCatalogSha256': old_catalog_sha,
            'ruleOrder': 1, 'firstForRule': True, 'firstRecordId': 'historical-record',
            'stageEvidence': {name: {'path': name + '.json', 'sha256': str(index) * 64}
                              for index, name in enumerate(('static', 'regression', 'runtime'), 1)},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'acceptance.json'
            path.write_text(json.dumps({'schemaVersion': 1, 'appId': 'qq', 'records': [record]}))
            with patch('scripts.ci.acceptance.load_rule_catalog', return_value=static['ruleCatalog']):
                rows = load_records(path, 'qq', initial_migration=False)
        self.assertEqual(['historical-record'], [row['recordId'] for row in rows])

    def test_forward_binding_proposal_caps_only_the_validated_old_range(self):
        bindings = [{'ruleId': 'rule_a', 'min': 100, 'max': None}]
        records = [
            {'expectedPlan': {'ruleId': 'rule_a'}, 'identity': {'versionCode': 140}},
            {'expectedPlan': {'ruleId': 'rule_a'}, 'identity': {'versionCode': 180}},
        ]
        self.assertEqual(
            [{'ruleId': 'rule_a', 'min': 100, 'max': 180},
             {'ruleId': 'rule_b', 'min': 220, 'max': None}],
            acceptance.propose_forward_binding(bindings, records, 220, 'rule_b'),
        )

    def test_forward_binding_requires_human_routing_review_at_or_below_history(self):
        records = [{'expectedPlan': {'ruleId': 'rule_a'}, 'identity': {'versionCode': 180}}]
        for candidate_version in (180, 179):
            with self.subTest(candidate_version=candidate_version), \
                 self.assertRaisesRegex(acceptance.RoutingReviewRequired, 'ROUTING_REVIEW_REQUIRED'):
                acceptance.propose_forward_binding(
                    [{'ruleId': 'rule_a', 'min': 100, 'max': None}], records,
                    candidate_version, 'rule_b')

    def test_forward_binding_first_rule_uses_candidate_baseline_and_rejects_reuse(self):
        self.assertEqual([{'ruleId': 'first_rule', 'min': 220, 'max': None}],
                         acceptance.propose_forward_binding([], [], 220, 'first_rule'))
        with self.assertRaisesRegex(ValueError, 'already exists'):
            acceptance.propose_forward_binding(
                [{'ruleId': 'rule_a', 'min': 1, 'max': None}], [], 2, 'rule_a')

    def test_representatives_use_fixed_first_record_and_deduplicate_sha(self):
        records = [
            {'recordId': 'r1-first', 'firstRecordId': 'r1-first', 'appId': 'qq',
             'expectedPlan': {'ruleId': 'r1'}, 'ruleOrder': 1, 'firstForRule': True,
             'identity': {'apkSha256': 'a' * 64}},
            {'recordId': 'r1-later', 'firstRecordId': 'r1-first', 'appId': 'qq',
             'expectedPlan': {'ruleId': 'r1'}, 'ruleOrder': 1, 'firstForRule': False,
             'identity': {'apkSha256': 'b' * 64}},
            {'recordId': 'r2-first', 'firstRecordId': 'r2-first', 'appId': 'qq',
             'expectedPlan': {'ruleId': 'r2'}, 'ruleOrder': 2, 'firstForRule': True,
             'identity': {'apkSha256': 'c' * 64}},
            {'recordId': 'r3-first', 'firstRecordId': 'r3-first', 'appId': 'qq',
             'expectedPlan': {'ruleId': 'r3'}, 'ruleOrder': 3, 'firstForRule': True,
             'identity': {'apkSha256': 'd' * 64}},
            {'recordId': 'r4-first', 'firstRecordId': 'r4-first', 'appId': 'qq',
             'expectedPlan': {'ruleId': 'r4'}, 'ruleOrder': 4, 'firstForRule': True,
             'identity': {'apkSha256': 'c' * 64}},
        ]
        self.assertEqual(['r2-first', 'r3-first'],
                         [record['recordId'] for record in select_representatives(records)])

    def test_representative_selection_rejects_missing_or_replaced_first_record(self):
        malformed = [{'recordId': 'r1-second', 'firstRecordId': 'r1-first', 'appId': 'qq',
                      'expectedPlan': {'ruleId': 'r1'}, 'ruleOrder': 1, 'firstForRule': False,
                      'identity': {'apkSha256': 'a' * 64}}]
        with self.assertRaises(ValueError):
            select_representatives(malformed)

    def test_record_order_comes_from_validated_snapshot_and_rejects_duplicate_sha(self):
        records = [
            {'recordId': 'r1-first', 'firstRecordId': 'r1-first', 'appId': 'qq',
             'expectedPlan': {'ruleId': 'r1'}, 'ruleOrder': 1, 'firstForRule': True,
             'identity': {'apkSha256': 'a' * 64}},
            {'recordId': 'r1-second', 'firstRecordId': 'r1-first', 'appId': 'qq',
             'expectedPlan': {'ruleId': 'r1'}, 'ruleOrder': 1, 'firstForRule': False,
             'identity': {'apkSha256': 'b' * 64}},
            {'recordId': 'r2-first', 'firstRecordId': 'r2-first', 'appId': 'qq',
             'expectedPlan': {'ruleId': 'r2'}, 'ruleOrder': 2, 'firstForRule': True,
             'identity': {'apkSha256': 'c' * 64}},
        ]
        self.assertEqual({'firstForRule': False, 'firstRecordId': 'r1-first', 'ruleOrder': 1},
                         acceptance.derive_record_order(records, 'r1', 'later-r1'))
        self.assertEqual({'firstForRule': True, 'firstRecordId': 'new-r3', 'ruleOrder': 3},
                         acceptance.derive_record_order(records, 'r3', 'new-r3'))
        with self.assertRaisesRegex(ValueError, 'already present'):
            acceptance.derive_record_order(records, 'r3', 'duplicate', 'a' * 64)
        records[1]['firstRecordId'] = 'wrong-record'
        with self.assertRaisesRegex(ValueError, 'malformed'):
            acceptance.derive_record_order(records, 'r3', 'new-r3')

    def fixture(self):
        from scripts.test_qq_requests import expectation, proof
        identity = {'packageName': 'com.tencent.mobileqq', 'versionName': '9.3.1',
                    'versionCode': 14378, 'abi': 'arm64-v8a', 'apkSha256': 'a' * 64,
                    'signerSha256': get_policy('qq').signer_sha256}
        plan = {'selection': 'UNIQUE', 'ruleId': 'qq_device_type_001',
                'strategyId': 'qq_tablet_001', 'targets': {'device_type': {
                    'descriptor': 'Lx/Y;->a()V', 'shape': {'parameters': [],
                    'returnType': 'void', 'isStatic': True}}}}
        catalog = {'schemaVersion': 1, 'appId': 'qq', 'ruleCatalogSha256': 'd' * 64,
                   'definitions': [{'ruleId': 'qq_device_type_001', 'strategyId': 'qq_tablet_001',
                                   'requiredTargetIds': ['device_type']}],
                   'bindings': [{'ruleId': 'qq_device_type_001', 'min': 14378, 'max': None}],
                   'strategyIds': ['qq_tablet_001']}
        route = {'appId': 'qq', 'candidates': ['qq_device_type_001']}
        route_sha = hashlib.sha256(json.dumps(route, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        candidate = {'schemaVersion': 1, 'appId': 'qq', 'status': 'PREPARED',
                     'identity': identity,
                     'apkFiles': [{'path': 'apk-inputs/qq.apk', 'role': 'base', 'sha256': 'a' * 64}],
                     'sourceUrl': 'https://downv6.qq.com/qqweb/QQ_1/android_apk/9.3.1.apk',
                     'expectedPlan': plan, 'staticReport': 'static-check.json',
                     'staticReportSha256': '3' * 64, 'ruleCatalogSha256': 'd' * 64,
                     'route': dict(route, samplingMode='DIRECT'), 'routeSha256': route_sha}
        candidate['requestIdentityExpectation'] = expectation('a' * 64)
        module = {'sha256': 'b' * 64, 'sourceTreeSha256': 'c' * 64,
                  'ruleCatalogSha256': 'd' * 64, 'routeSha256': route_sha,
                  'routeConfigurationSha256': '7' * 64, 'fingerprintFileSha256': '8' * 64,
                  'dependencyLockSha256': 'f' * 64,
                  'fingerprint': {'schemaVersion': 1, 'sourceTreeSha256': 'c' * 64,
                                  'routeConfigurationSha256': '7' * 64, 'dependencyLockSha256': 'f' * 64},
                  'route': route,
                  'dexkit': {'dexkitAndroidApiSha256': '1' * 64,
                             'dexkitAndroidArm64NativeSha256': '2' * 64}}
        static = {'appId': 'qq', 'analysisScope': 'STATIC_ONLY', 'identity': identity,
                  'inputHashes': [{'path': '/candidate/qq.apk', 'sha256': 'a' * 64}],
                  'ruleCatalogSha256': 'd' * 64, 'ruleCatalog': catalog,
                  'executionIdentity': {'ruleCatalogSha256': 'd' * 64, 'fingerprintFileSha256': '8' * 64,
                                        'sourceTreeSha256': 'c' * 64, 'routeConfigurationSha256': '7' * 64,
                                        'dependencyLockSha256': 'f' * 64},
                  'routeCandidates': ['qq_device_type_001'],
                  'selection': {'status': 'SELECTED', 'selectedRuleId': 'qq_device_type_001',
                                'strategyId': 'qq_tablet_001', 'targets': copy.deepcopy(plan['targets'])}}
        regression = {'status': 'NO_HISTORICAL_SAMPLES', 'records': [],
                      'evidence': {'path': 'regression/results.json', 'sha256': '4' * 64}}
        smoke = {'status': 'RUNTIME_SMOKE_VERIFIED', 'appId': 'qq', 'packageName': identity['packageName'],
                 'abi': identity['abi'], 'apkSha256': 'a' * 64, 'moduleApkSha256': 'b' * 64,
                 'moduleSourceTreeSha256': 'c' * 64, 'ruleCatalogSha256': 'd' * 64,
                 'routeSha256': route_sha, 'dependencyLockSha256': 'f' * 64,
                 'acceptanceKind': 'REQUEST_APP_ID_CONTRAST',
                 'nativeEvidence': {'status': 'LOADED', 'apiSha256': '1' * 64, 'nativeSha256': '2' * 64},
                 'qfixEvidence': {'status': 'READY', 'contextPackage': identity['packageName'],
                                  'classLoader': 'com.tencent.mobileqq'},
                 'timingEvidence': {'qfixReadyBeforeNative': True, 'hookInstalledBeforeRequest': True},
                 'requestIdentityExpectation': expectation('a' * 64),
                 'requestIdentityEvidence': proof('a' * 64, 'b' * 64),
                 'evidence': [{'path': 'apps/qq/logcat-enabled.txt', 'sha256': '1' * 64}],
                 'evidenceReport': {'path': 'evidence/smoke-report.json', 'sha256': '5' * 64}}
        return candidate, module, static, regression, smoke

    def test_candidate_pass_does_not_override_regression_failure(self):
        candidate, module, static, regression, smoke = self.fixture()
        regression['status'] = 'FAILED'
        result = evaluate_validation(candidate, module, static, regression, smoke, 'success')
        self.assertEqual('REGRESSION_FAILED', result['outcome'])

    def test_evaluator_result_does_not_claim_boolean_trust(self):
        candidate, module, static, regression, smoke = self.fixture()
        result = evaluate_validation(candidate, module, static, regression, smoke, 'success')
        self.assertNotIn('trustedEvidence', result)

    def test_validation_result_keeps_static_regression_and_runtime_report_references(self):
        candidate, module, static, regression, smoke = self.fixture()
        result = evaluate_validation(candidate, module, static, regression, smoke, 'success')
        self.assertEqual({'static': {'path': 'static-check.json', 'sha256': '3' * 64},
                          'regression': regression['evidence'], 'runtime': smoke['evidenceReport']},
                         result['stageEvidence'])

    def test_static_plan_mismatch_is_rejected_without_mutating_inputs(self):
        candidate, module, static, regression, smoke = self.fixture()
        before = copy.deepcopy((candidate, module, static, regression, smoke))
        candidate['expectedPlan']['ruleId'] = 'unexpected-rule'
        result = evaluate_validation(candidate, module, static, regression, smoke, 'success')
        self.assertEqual('CANDIDATE_REJECTED', result['outcome'])
        self.assertEqual('unexpected-rule', candidate['expectedPlan']['ruleId'])
        self.assertEqual(before[1], module)
        self.assertEqual(before[2], static)
        self.assertEqual(before[3], regression)
        self.assertEqual(before[4], smoke)

    def test_unavailable_cancelled_skipped_and_missing_evidence_are_not_verified(self):
        for smoke, conclusion in [({'status': 'ACCEPTANCE_UNAVAILABLE'}, 'success'),
                                  ({}, 'cancelled'), ({}, 'skipped'), ({}, 'success')]:
            candidate, module, static, regression, _ = self.fixture()
            self.assertNotEqual('VERIFIED', evaluate_validation(
                candidate, module, static, regression, smoke, conclusion)['outcome'])

    def test_mismatched_module_or_candidate_hash_is_not_verified(self):
        for field, value in [('sha256', '9' * 64), ('sourceTreeSha256', '8' * 64),
                             ('ruleCatalogSha256', '7' * 64), ('routeSha256', '6' * 64)]:
            candidate, module, static, regression, smoke = self.fixture()
            module[field] = value
            result = evaluate_validation(candidate, module, static, regression, smoke, 'success')
            self.assertNotEqual('VERIFIED', result['outcome'], field)

    def test_candidate_route_is_recomputed_from_the_shared_static_report(self):
        candidate, module, static, regression, smoke = self.fixture()
        candidate['route']['candidates'] = ['unlisted-rule']
        result = evaluate_validation(candidate, module, static, regression, smoke, 'success')
        self.assertNotEqual('VERIFIED', result['outcome'])

    def test_record_draft_revalidates_files_and_never_writes_acceptance_table(self):
        candidate, module, static, regression, smoke = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            acceptance_path = root / 'acceptance.json'
            raw_snapshot = b'{"schemaVersion":1,"appId":"qq","records":[]}\n'
            snapshot = AcceptanceSnapshot(acceptance_path.resolve(), raw_snapshot,
                                          hashlib.sha256(raw_snapshot).hexdigest(), b'[]', True)
            validation = evaluate_validation(candidate, module, static, regression, smoke, 'success')
            validation['acceptanceSnapshot'] = {'path': str(acceptance_path.resolve()),
                                                'sha256': snapshot.sha256,
                                                'recordIds': [], 'initialMigration': True}
            validation['_acceptanceSnapshotObject'] = snapshot
            with patch('scripts.ci.runtime_task.verify_validation_files', return_value=validation) as verifier:
                record = make_record(candidate, module, validation, candidate_path=root / 'candidate.json',
                                     module_apk=root / 'module.apk', regression_path=root / 'regression.json',
                                     smoke_path=root / 'smoke.json', evidence_root=root,
                                     acceptance_path=acceptance_path, initial_migration=True)
            self.assertIs(snapshot, verifier.call_args.kwargs['acceptance_snapshot'])
            self.assertEqual(candidate['identity'], record['identity'])
            self.assertEqual('VERIFIED', record['status'])
            self.assertEqual(record['recordId'], record['firstRecordId'])
            self.assertFalse((root / 'acceptance.json').exists())

    def test_make_record_rejects_forged_verified_dictionary_without_files(self):
        candidate, module, _, _, _ = self.fixture()
        forged = {'outcome': 'VERIFIED', 'trustedEvidence': True,
                  'candidate': candidate, 'module': module,
                  'runtime': {'evidence': [{'path': 'missing.json', 'sha256': '1' * 64}]}}
        with self.assertRaisesRegex(ValueError, 'requires candidate'):
            make_record(candidate, module, forged)

    def test_make_record_rejects_stage_files_replaced_after_evaluation(self):
        candidate, module, static, regression, smoke = self.fixture()
        validation = evaluate_validation(candidate, module, static, regression, smoke, 'success')
        changed = dict(validation, stageEvidence={**validation['stageEvidence'],
                                                  'runtime': {'path': 'evidence/smoke-report.json',
                                                              'sha256': '9' * 64}})
        with tempfile.TemporaryDirectory() as temporary, \
             patch('scripts.ci.runtime_task.verify_validation_files', return_value=validation):
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, 'changed after unified validation'):
                make_record(candidate, module, changed, candidate_path=root / 'candidate.json',
                            module_apk=root / 'module.apk', regression_path=root / 'regression.json',
                            smoke_path=root / 'smoke.json', evidence_root=root,
                            acceptance_path=root / 'acceptance.json', initial_migration=True)

    def test_make_record_rejects_validation_bound_to_a_different_acceptance_snapshot(self):
        candidate, module, static, regression, smoke = self.fixture()
        validation = evaluate_validation(candidate, module, static, regression, smoke, 'success')
        validation['acceptanceSnapshot'] = {'path': '/repo/compatibility/qq/acceptance.json',
                                            'sha256': '1' * 64, 'recordIds': ['old-record'],
                                            'initialMigration': False}
        snapshot = AcceptanceSnapshot(Path('/repo/compatibility/qq/acceptance.json'), b'{}',
                                      hashlib.sha256(b'{}').hexdigest(), b'[]', False)
        validation['_acceptanceSnapshotObject'] = snapshot
        rebuilt = copy.deepcopy(validation)
        rebuilt['_acceptanceSnapshotObject'] = snapshot
        rebuilt['acceptanceSnapshot']['sha256'] = '2' * 64
        with tempfile.TemporaryDirectory() as temporary, \
             patch('scripts.ci.runtime_task.verify_validation_files', return_value=rebuilt):
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, 'changed after unified validation'):
                make_record(candidate, module, validation, candidate_path=root / 'candidate.json',
                            module_apk=root / 'module.apk', regression_path=root / 'regression.json',
                            smoke_path=root / 'smoke.json', evidence_root=root,
                            acceptance_path=root / 'acceptance.json', initial_migration=False)


if __name__ == '__main__':
    unittest.main()
