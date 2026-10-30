import copy
import unittest

from scripts.ci.semantic_protocol import (catalog_sha256, plan_execution_sha256,
    snapshot_sha256, validate_catalog, validate_complete_plan, validate_snapshot)


def catalog():
    row = {'schemaVersion': 2, 'appId': 'qq', 'ruleCatalogSha256': '',
        'contractId': 'qq_tablet_device_type', 'contractRevision': 1,
        'requiredRoles': ['device_type'], 'recognizerIds': ['qq_cached_device_enum'],
        'strategies': [{'id': 'qq_tablet_enum', 'revision': 1,
            'roles': [{'role': 'device_type', 'phase': 'AROUND', 'configuration': {'enum': 'TABLET'}}]}],
        'versionHints': []}
    row['ruleCatalogSha256'] = catalog_sha256(row)
    return row


def snapshot():
    row = {'protocolVersion': 1, 'readyLoaderId': 'loader-0', 'contentSha256': '',
        'loaders': [{'id': 'loader-0', 'parentId': None, 'delegation': 'PARENT_FIRST', 'dexSha256': ['a' * 64]}],
        'archives': [{'loaderId': 'loader-0', 'containerIndex': 0, 'kind': 'ZIP', 'containerSha256': 'b' * 64, 'dexSha256': ['a' * 64]}]}
    row['contentSha256'] = snapshot_sha256(row)
    return row


def plan():
    return {'schemaVersion': 1, 'snapshotSha256': snapshot()['contentSha256'],
        'contractId': 'qq_tablet_device_type', 'contractRevision': 1,
        'strategyId': 'qq_tablet_enum', 'strategyRevision': 1, 'hooks': [{
            'role': 'device_type',
            'descriptor': 'Lcom/tencent/common/config/pad/PadUtil;->a(Landroid/content/Context;)Lcom/tencent/common/config/pad/DeviceType;',
            'shape': {'parameters': ['android.content.Context'], 'returnType': 'com.tencent.common.config.pad.DeviceType', 'isStatic': True},
            'source': {'loaderId': 'loader-0', 'dexIndex': 0, 'dexSha256': 'a' * 64},
            'phase': 'AROUND', 'configuration': {'enum': 'TABLET'}}]}


class SemanticProtocolTest(unittest.TestCase):
    def test_unicode_configuration_order_matches_kotlin_utf16_golden(self):
        value = {'schemaVersion': 1, 'snapshotSha256': '0' * 64,
            'contractId': 'unicode-contract', 'contractRevision': 1,
            'strategyId': 'unicode-strategy', 'strategyRevision': 1, 'hooks': [{
                'role': 'role', 'descriptor': 'Lfixture/Type;->isTablet()Z',
                'shape': {'parameters': [], 'returnType': 'boolean', 'isStatic': True},
                'source': {'loaderId': 'loader-0', 'dexIndex': 0, 'dexSha256': 'a' * 64},
                'phase': 'AROUND', 'configuration': {'\ue000': 'bmp', '😀': 'astral'}}]}
        self.assertEqual('4bd042c3477d4fe16230ff978adc9adbb0bc78b92df900d95760de2fb3db3bb2', plan_execution_sha256(value))

    def test_execution_encoding_matches_independent_android_kotlin_golden(self):
        value = plan()
        value['snapshotSha256'] = '44975b792df81c313e2fb46fabde800aed4eb5a11c90263c8f2918916e86761e'
        value['hooks'][0]['source']['dexSha256'] = '249c1fa30b2df8aeb0dad19fc94dfda045dd5c03c2e9112543cddee3fd35a7ba'
        self.assertEqual('2133c13bc63f511f1ea618f983e2e85744b20f54d4ef56ba36bbd2f05e023709', plan_execution_sha256(value))
        upper = copy.deepcopy(value)
        upper['hooks'][0]['source']['dexSha256'] = upper['hooks'][0]['source']['dexSha256'].upper()
        self.assertEqual(plan_execution_sha256(value), plan_execution_sha256(upper))

    def test_validates_complete_registered_plan_and_all_source_indices(self):
        validate_complete_plan(plan(), catalog(), snapshot())
        changes = [lambda x: x.update(strategyRevision=2),
            lambda x: x['hooks'][0].update(phase='BEFORE'),
            lambda x: x['hooks'][0].update(configuration={'enum': 'PHONE'}),
            lambda x: x['hooks'][0]['source'].update(dexIndex=1),
            lambda x: x['hooks'][0]['source'].update(dexSha256='c' * 64),
            lambda x: x.update(snapshotSha256='f' * 64),
            lambda x: x['hooks'][0]['shape'].update(parameters=[]),
            lambda x: x.update(hooks=x['hooks'] * 2)]
        for change in changes:
            value = plan(); change(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_complete_plan(value, catalog(), snapshot())

    def test_rejects_partial_contradictory_and_gapped_archive_manifests(self):
        validate_snapshot(snapshot(), [{'path': 'base.apk', 'sha256': 'b' * 64}])
        changes = [lambda x: x.update(archives=[]),
            lambda x: x['archives'][0].update(containerIndex=1),
            lambda x: x['archives'][0].update(dexSha256=['f' * 64]),
            lambda x: x['archives'][0].update(kind='DEX'),
            lambda x: x['loaders'][0].update(parentId='missing'),
            lambda x: x['loaders'][0].update(parentId='loader-0'),
            lambda x: x.update(contentSha256='f' * 64)]
        for change in changes:
            value = snapshot(); change(value)
            with self.subTest(value=value), self.assertRaises(ValueError): validate_snapshot(value)
        with self.assertRaises(ValueError): validate_snapshot(snapshot(), [{'path': 'base.apk', 'sha256': 'f' * 64}])

    def test_order_changes_identity_and_paths_do_not_enter_snapshot_encoding(self):
        value = snapshot()
        value['loaders'][0]['dexSha256'] += ['c' * 64]
        other = copy.deepcopy(value)
        other['loaders'][0]['dexSha256'].reverse()
        self.assertNotEqual(snapshot_sha256(value), snapshot_sha256(other))
        validate_snapshot(snapshot(), [{'path': '/another/location.apk', 'sha256': 'b' * 64}])

    def test_catalog_hints_never_exclude_registered_queries_or_hide_contract_changes(self):
        value = catalog(); validate_catalog(value, 'qq')
        for change in (lambda x: x.update(contractRevision=2),
                lambda x: x.update(recognizerIds=[]),
                lambda x: x.update(versionHints=[{'recognizerId': 'unregistered', 'min': 1, 'max': None}])):
            value = catalog(); change(value)
            with self.assertRaises(ValueError): validate_catalog(value)
        value = catalog()
        value['versionHints'] = [{'recognizerId': 'qq_cached_device_enum', 'min': 1, 'max': None},
            {'recognizerId': 'qq_cached_device_enum', 'min': 2, 'max': 3}]
        value['ruleCatalogSha256'] = catalog_sha256(value)
        validate_catalog(value)
        with self.assertRaises(ValueError): validate_catalog(value, 'wechat')

    def test_rejects_bool_numbers_and_extra_execution_fields(self):
        for field in ('schemaVersion', 'contractRevision', 'strategyRevision'):
            value = plan(); value[field] = True
            with self.assertRaises(ValueError): plan_execution_sha256(value)
        value = plan(); value['unusedMetadata'] = 'do not ignore unknown execution fields'
        with self.assertRaises(ValueError): plan_execution_sha256(value)




def check_report():
    from scripts.ci.semantic_protocol import execution_identity_sha256
    from scripts.ci.app_policy import get_policy
    fingerprint = {key: 'd' * 64 for key in (
        'ruleCatalogSha256', 'ruleImplementationSha256', 'sharedResolverImplementationSha256',
        'dexkitLockSha256', 'dexkitApiSha256', 'dexkitAndroidApiSha256',
        'dexkitLinuxAarch64NativeSha256', 'dexkitLinuxX8664NativeSha256',
        'dexkitAndroidArm64NativeSha256', 'strategyImplementationSha256',
        'cacheIdentitySha256', 'sourceTreeSha256', 'routeConfigurationSha256',
        'dependencyLockSha256', 'fingerprintFileSha256')}
    fingerprint.update(ruleCatalogSha256=catalog()['ruleCatalogSha256'], dexkitSourceCommit='a' * 40,
        dexkitNativeSha256=None, dexkitPlatform=None)
    fingerprint['cacheIdentitySha256'] = execution_identity_sha256(fingerprint)
    return {'schemaVersion': 2, 'analysisScope': 'APK_FILE_SNAPSHOT', 'appId': 'qq',
        'identity': {'packageName': get_policy('qq').package_name, 'versionName': 'test',
            'versionCode': 14378, 'abi': 'arm64-v8a', 'apkSha256': 'b' * 64,
            'signerSha256': get_policy('qq').signer_sha256},
        'inputHashes': [{'path': 'base.apk', 'sha256': 'b' * 64}],
        'ruleCatalog': catalog(), 'executionIdentity': fingerprint, 'snapshot': snapshot(),
        'evaluatedRecognizerIds': ['qq_cached_device_enum'],
        'recognizers': [{'recognizerId': 'qq_cached_device_enum', 'status': 'PLANS',
            'plans': [{'plan': plan(), 'executionSha256': plan_execution_sha256(plan()), 'evidence': ['structure']}],
            'diagnostics': [], 'reason': None}],
        'selection': {'status': 'SELECTED', 'plan': plan(), 'executionSha256': plan_execution_sha256(plan()),
            'evidenceByRecognizer': {'qq_cached_device_enum': ['structure']},
            'alternatives': [], 'diagnostics': {}, 'incompleteReasons': {}}}


class SemanticReportTest(unittest.TestCase):
    def validate(self, value):
        from scripts.ci.semantic_protocol import validate_check_report
        validate_check_report(value, expected_app_id='qq')

    def test_report_selection_is_recomputed_from_all_recognizer_results(self):
        value = check_report(); self.validate(value)
        for mutate in (
            lambda r: r['recognizers'][0].update(status='INCOMPLETE', reason='budget exhausted'),
            lambda r: r['recognizers'][0].update(status='NO_MATCH', plans=[]),
            lambda r: r['selection'].update(executionSha256='f' * 64),
            lambda r: r['selection'].update(evidenceByRecognizer={'qq_cached_device_enum': ['invented']}),
            lambda r: r.update(recognizers=[]),
            lambda r: r.update(evaluatedRecognizerIds=['unregistered']),
            lambda r: r['executionIdentity'].update(sharedResolverImplementationSha256='f' * 64),
        ):
            value = check_report(); mutate(value)
            with self.subTest(report=value), self.assertRaises(ValueError): self.validate(value)

    def test_incomplete_with_a_complete_plan_is_never_selected(self):
        value = check_report()
        value['recognizers'][0].update(status='INCOMPLETE', reason='budget exhausted')
        selected = value['selection']
        selected['alternatives'] = [{'plan': selected['plan'], 'executionSha256': selected['executionSha256'],
            'evidenceByRecognizer': selected['evidenceByRecognizer']}]
        selected.update(status='INCOMPLETE', plan=None, executionSha256=None,
            evidenceByRecognizer={}, incompleteReasons={'qq_cached_device_enum': 'budget exhausted'})
        self.validate(value)
        selected.update(status='SELECTED', plan=plan(), executionSha256=plan_execution_sha256(plan()))
        with self.assertRaises(ValueError): self.validate(value)

    def test_distinct_complete_plans_conflict_including_incomplete_discoveries(self):
        value = check_report()
        second = plan(); second['hooks'][0]['descriptor'] = second['hooks'][0]['descriptor'].replace('->a(', '->b(')
        value['recognizers'][0]['plans'].append({'plan': second, 'executionSha256': plan_execution_sha256(second), 'evidence': ['second']})
        alternatives = [{'plan': row['plan'], 'executionSha256': row['executionSha256'],
            'evidenceByRecognizer': {'qq_cached_device_enum': row['evidence']}}
            for row in value['recognizers'][0]['plans']]
        value['selection'].update(status='CONFLICT', plan=None, executionSha256=None,
            evidenceByRecognizer={}, alternatives=sorted(alternatives, key=lambda row: row['executionSha256']))
        self.validate(value)
        value['recognizers'][0].update(status='INCOMPLETE', reason='budget exhausted')
        self.validate(value)
        value['selection']['status'] = 'INCOMPLETE'
        with self.assertRaises(ValueError): self.validate(value)

    def test_every_registered_query_must_be_present_even_without_a_version_hint(self):
        from scripts.ci.semantic_protocol import execution_identity_sha256
        value = check_report()
        value['ruleCatalog']['recognizerIds'].append('second_query')
        value['ruleCatalog']['ruleCatalogSha256'] = catalog_sha256(value['ruleCatalog'])
        value['executionIdentity']['ruleCatalogSha256'] = value['ruleCatalog']['ruleCatalogSha256']
        value['executionIdentity']['cacheIdentitySha256'] = execution_identity_sha256(value['executionIdentity'])
        with self.assertRaises(ValueError): self.validate(value)
        value['recognizers'].append({'recognizerId': 'second_query', 'status': 'NO_MATCH', 'plans': [], 'diagnostics': [], 'reason': None})
        value['evaluatedRecognizerIds'].append('second_query')
        self.validate(value)
        value['recognizers'][-1].update(status='EVALUATION_ERROR', diagnostics=['parse failed'])
        value['selection'].update(status='EVALUATION_ERROR', plan=None, executionSha256=None,
            evidenceByRecognizer={}, diagnostics={'second_query': ['parse failed']})
        self.validate(value)

    def test_wrong_identity_snapshot_catalog_and_scope_cannot_be_hidden_by_selected(self):
        for mutate in (
            lambda r: r['identity'].update(signerSha256='f' * 64),
            lambda r: r['identity'].update(apkSha256='f' * 64),
            lambda r: r.update(analysisScope='RUNTIME_VERIFIED'),
            lambda r: r.update(appId='wechat'),
            lambda r: r['snapshot']['archives'][0].update(containerSha256='f' * 64),
        ):
            value = check_report(); mutate(value)
            with self.assertRaises(ValueError): self.validate(value)


class SemanticMalformedScalarTest(unittest.TestCase):
    def test_non_scalar_enum_and_loader_ids_are_validation_errors(self):
        changes = [lambda value: value['loaders'][0].update(delegation=[]),
                   lambda value: value['archives'][0].update(kind={}),
                   lambda value: value['archives'][0].update(loaderId=[])]
        for change in changes:
            value = snapshot(); change(value)
            with self.assertRaises(ValueError): validate_snapshot(value)
        value = catalog(); value['strategies'][0]['roles'][0]['phase'] = []
        with self.assertRaises(ValueError): validate_catalog(value)
        value = plan(); value['hooks'][0]['phase'] = {}
        with self.assertRaises(ValueError): plan_execution_sha256(value)


if __name__ == '__main__': unittest.main()
