import copy
import json
import unittest
from pathlib import Path

from scripts.ci.regression_groups import GROUPING_VERSION, select_regression_groups


ROOT = Path(__file__).resolve().parents[1]


def case(app, version, record_id, apk='a', accepted='2026-01-01T00:00:00Z'):
    recognizer = app + '_recognizer'
    plan = {
        'schemaVersion': 1, 'snapshotSha256': 'f' * 64,
        'contractId': app + '_contract', 'contractRevision': 2,
        'strategyId': app + '_strategy', 'strategyRevision': 3,
        'hooks': [{'role': 'main', 'descriptor': 'Lx/Obf;->a()Z',
                   'shape': {'parameters': [], 'returnType': 'boolean', 'isStatic': True},
                   'phase': 'AFTER', 'configuration': {'mode': 'enabled'}}],
    }
    return {
        'kind': 'ACCEPTED_V2', 'recordId': record_id, 'appId': app,
        'identity': {'versionCode': version}, 'acceptedAt': accepted,
        'apkFiles': [{'role': 'base', 'sha256': apk * 64}],
        'savedStaticReport': {
            'selection': {'plan': plan, 'evidenceByRecognizer': {recognizer: []}},
        },
    }


class RegressionGroupsTest(unittest.TestCase):
    def test_groups_identical_behavior_and_deduplicates_apk(self):
        older = case('qq', 10, 'old', 'a')
        duplicate = case('qq', 11, 'dup', 'a', '2026-02-01T00:00:00Z')
        other = case('qq', 9, 'other', 'b')
        result = select_regression_groups([older, duplicate, other])
        self.assertEqual(GROUPING_VERSION, result['version'])
        self.assertEqual(1, len(result['selectedGroups']))
        self.assertEqual(['dup', 'other'], [c['recordId'] for c in result['selectedGroups'][0]['cases']])
        self.assertEqual('dup', result['selectedGroups'][0]['preferredCase']['recordId'])

    def test_distinguishes_hook_shape_phase_configuration_and_revision(self):
        original = case('qq', 10, 'a')
        variants = []
        for name, mutate in [
            ('shape', lambda p: p['hooks'][0]['shape'].update(isStatic=False)),
            ('phase', lambda p: p['hooks'][0].update(phase='BEFORE')),
            ('config', lambda p: p['hooks'][0]['configuration'].update(mode='other')),
            ('revision', lambda p: p.update(strategyRevision=4)),
        ]:
            item = case('qq', 9, name, name[0])
            mutate(item['savedStaticReport']['selection']['plan'])
            variants.append(item)
        groups = select_regression_groups([original, *variants])['groups']
        self.assertEqual(5, len(groups))

    def test_wechat_monitor_field_uses_class_field_relationship_only(self):
        a = case('wechat', 10, 'a')
        b = copy.deepcopy(a); b['recordId'] = 'b'; b['apkFiles'][0]['sha256'] = 'b' * 64
        for item, descriptor in ((a, 'Lx/One;->f:Ljava/lang/Object;'), (b, 'Ly/Two;->q:Ljava/lang/Object;')):
            item['savedStaticReport']['selection']['plan']['hooks'][0]['configuration'] = {
                'policy': 'cache_true_after', 'cacheField': 'Lobf/C;->c:Ljava/lang/Boolean;',
                'monitorField': descriptor}
        for item in (a, b):
            item['savedStaticReport']['selection']['behaviorFeatures'] = {'wrapperMode': 'inline'}
        self.assertEqual(1, len(select_regression_groups([a, b])['groups']))

    def test_wechat_missing_wrapper_mode_isolated_per_apk(self):
        a = case('wechat', 10, 'a', 'a')
        b = case('wechat', 9, 'b', 'b')
        result = select_regression_groups([a, b])
        self.assertEqual(2, len(result['groups']))
        self.assertTrue(all(not group['evidenceComplete'] for group in result['groups']))

    def test_wechat_machine_readable_wrapper_mode_groups_and_splits(self):
        a = case('wechat', 10, 'a', 'a')
        b = case('wechat', 9, 'b', 'b')
        c = case('wechat', 8, 'c', 'c')
        for item, mode in ((a, 'aggregate'), (b, 'aggregate'), (c, 'inline')):
            item['savedStaticReport']['selection']['behaviorFeatures'] = {'wrapperMode': mode}
        self.assertEqual(2, len(select_regression_groups([a, b, c])['groups']))

    def test_cache_field_ignores_obfuscated_owner_and_field_names(self):
        a = case('qq', 10, 'a', 'a')
        b = copy.deepcopy(a); b['recordId'] = 'b'; b['identity']['versionCode'] = 9; b['apkFiles'][0]['sha256'] = 'b' * 64
        for item, value in ((a, 'Lx/A;->m:Ljava/lang/Object;'), (b, 'Ly/B;->n:Ljava/lang/Object;')):
            item['savedStaticReport']['selection']['plan']['hooks'][0]['configuration'] = {'cacheField': value}
        self.assertEqual(1, len(select_regression_groups([a, b])['groups']))

    def test_monitor_binding_distinguishes_class_and_field_shapes(self):
        base = case('wechat', 10, 'base', 'a')
        plan = base['savedStaticReport']['selection']['plan']
        plan['hooks'][0]['descriptor'] = 'Lx/Hook;->run()Z'
        variants = []
        for name, monitor in (('class', 'Lx/Hook;'), ('same', 'Lx/Hook;->lock:Ljava/lang/Object;'), ('other', 'Ly/Lock;->lock:Ljava/lang/Object;')):
            item = copy.deepcopy(base); item['recordId'] = name; item['apkFiles'][0]['sha256'] = name[0] * 64
            item['savedStaticReport']['selection']['plan']['hooks'][0]['configuration'] = {'monitorField': monitor}
            item['savedStaticReport']['selection']['behaviorFeatures'] = {'wrapperMode': 'inline'}
            variants.append(item)
        self.assertEqual(3, len(select_regression_groups(variants)['groups']))

    def test_duplicate_input_prefers_complete_record_before_newer_timestamp(self):
        complete = case('qq', 10, 'complete', 'a', '2026-01-01T00:00:00Z')
        incomplete = case('qq', 10, 'incomplete', 'a', '2026-02-01T00:00:00Z')
        del incomplete['savedStaticReport']['selection']['plan']['hooks'][0]['phase']
        result = select_regression_groups([incomplete, complete])
        self.assertEqual(['complete'], [c['recordId'] for group in result['groups'] for c in group['cases']])
        self.assertTrue(result['groups'][0]['evidenceComplete'])

    def test_duplicate_input_uses_parsed_timestamp_then_record_id(self):
        earlier = case('qq', 10, 'z', 'a', '2026-02-01T01:00:00+02:00')
        later = case('qq', 10, 'b', 'a', '2026-02-01T00:30:00Z')
        equal_time = case('qq', 10, 'a', 'a', '2026-02-01T00:30:00+00:00')
        for item in (earlier, later, equal_time):
            del item['savedStaticReport']['selection']['plan']['hooks'][0]['phase']
        result = select_regression_groups([earlier, later, equal_time])
        self.assertEqual(['b'], [c['recordId'] for group in result['groups'] for c in group['cases']])

    def test_obfuscated_dex_field_types_are_normalized(self):
        a = case('qq', 10, 'a', 'a')
        b = copy.deepcopy(a); b['recordId'] = 'b'; b['identity']['versionCode'] = 9; b['apkFiles'][0]['sha256'] = 'b' * 64
        for item, value in ((a, 'Lx/A;->cache:Lx/Obfuscated;'), (b, 'Lx/A;->other:Ly/Q;')):
            item['savedStaticReport']['selection']['plan']['hooks'][0]['configuration'] = {'cacheField': value}
        self.assertEqual(1, len(select_regression_groups([a, b])['groups']))

    def test_newest_acceptance_breaks_version_tie(self):
        older = case('qq', 10, 'older', 'a', '2026-01-01T00:00:00Z')
        newer = case('qq', 10, 'newer', 'b', '2026-02-01T00:00:00Z')
        groups = select_regression_groups([older, newer])['groups']
        self.assertEqual('newer', groups[0]['preferredCase']['recordId'])

    def test_rejects_mixed_app_ids(self):
        with self.assertRaisesRegex(ValueError, 'one appId'):
            select_regression_groups([case('qq', 10, 'a'), case('wecom', 10, 'b', 'b')])

    def test_missing_structure_preserves_per_apk_groups(self):
        a = case('wecom', 10, 'a')
        b = case('wecom', 9, 'b', 'b')
        del a['savedStaticReport']['selection']['plan']['hooks'][0]['phase']
        del b['savedStaticReport']['selection']['plan']['hooks'][0]['phase']
        result = select_regression_groups([a, b])
        self.assertEqual(2, len(result['groups']))
        self.assertTrue(all(g['evidenceComplete'] is False for g in result['groups']))

    def test_cap_and_deterministic_order(self):
        items = [case('qq', n, f'r{n}', str(n % 10)) for n in range(1, 6)]
        for index, item in enumerate(items):
            item['savedStaticReport']['selection']['plan']['strategyRevision'] = index + 10
        left = select_regression_groups(items)
        right = select_regression_groups(reversed(items))
        self.assertEqual([g['groupId'] for g in left['groups']], [g['groupId'] for g in right['groups']])
        self.assertEqual([5, 4, 3], [g['preferredCase']['identity']['versionCode'] for g in left['selectedGroups']])
        self.assertEqual(2, len(left['excludedGroups']))

    def test_current_v2_qq_and_wecom_outcomes(self):
        for app, expected in (('qq', 1), ('wecom', 1)):
            records = json.loads((ROOT / 'compatibility' / app / 'acceptance-v2.json').read_text())['records']
            cases = []
            for record in records:
                cases.append({'kind': 'ACCEPTED_V2', 'recordId': record['recordId'], 'appId': app,
                    'identity': record['identity'], 'apkFiles': record['apkFiles'],
                    'acceptedAt': record['acceptedAt'], 'savedStaticReport': record['staticReport']})
            result = select_regression_groups(cases)
            self.assertEqual(expected, len(result['groups']), app)
            unique_apks = {tuple(row['sha256'] for row in record['apkFiles']) for record in records}
            self.assertEqual(len(unique_apks), sum(len(g['cases']) for g in result['groups']))


if __name__ == '__main__':
    unittest.main()
