import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.ci.semantic_migration import load_migration_bundle, compare_fixed_report
from scripts.ci.semantic_protocol import execution_identity_sha256
from scripts.test_semantic_protocol import check_report

ROOT = Path(__file__).resolve().parents[1]


class SemanticMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / 'compatibility', self.root / 'compatibility')

    def report(self, profile):
        value = check_report()
        value.update(appId=profile['appId'], identity=profile['identity'],
            inputHashes=[{'path': f'input-{index}.apk', 'sha256': digest} for index, digest in enumerate(profile['apkInputSha256'])],
            ruleCatalog=profile['ruleCatalog'], snapshot=profile['snapshot'])
        identifiers = profile['ruleCatalog']['recognizerIds']
        self.assertEqual(1, len(identifiers))
        query = identifiers[0]
        value.update(evaluatedRecognizerIds=identifiers, recognizers=[{'recognizerId': query, 'status': 'PLANS',
            'plans': [{'plan': profile['expectedPlan'], 'executionSha256': profile['expectedExecutionSha256'], 'evidence': ['fixed shape']}],
            'diagnostics': [], 'reason': None}])
        value['selection'].update(plan=profile['expectedPlan'], executionSha256=profile['expectedExecutionSha256'], evidenceByRecognizer={query: ['fixed shape']})
        value['executionIdentity']['ruleCatalogSha256'] = profile['ruleCatalog']['ruleCatalogSha256']
        value['executionIdentity']['cacheIdentitySha256'] = execution_identity_sha256(value['executionIdentity'])
        return value

    def test_freezes_every_historical_representative_without_registering_new_acceptance(self):
        before = {app: (self.root / 'compatibility' / app / 'acceptance.json').read_bytes() for app in ('wechat', 'qq', 'wecom')}
        bundle = load_migration_bundle(self.root)
        self.assertEqual(4, len(bundle.profiles))
        self.assertEqual({'wechat', 'qq', 'wecom'}, {row['appId'] for row in bundle.profiles})
        self.assertEqual(before, {app: (self.root / 'compatibility' / app / 'acceptance.json').read_bytes() for app in before})
        mutated = bundle.profiles; mutated.clear()
        self.assertEqual(4, len(bundle.profiles))
        for profile in bundle.profiles:
            compare_fixed_report(bundle, profile['historicalRecordId'], self.report(profile))

    def test_changing_historical_bytes_or_omitting_a_fixed_representative_is_rejected(self):
        path = self.root / 'compatibility/wechat/acceptance.json'
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaises(ValueError): load_migration_bundle(self.root)
        shutil.copyfile(ROOT / 'compatibility/wechat/acceptance.json', path)
        expected = self.root / 'compatibility/semantic-migration/expectations.json'
        data = json.loads(expected.read_text()); data['profiles'].pop()
        expected.write_text(json.dumps(data))
        with self.assertRaises(ValueError): load_migration_bundle(self.root)

    def test_alternate_selected_target_cannot_replace_the_frozen_oracle(self):
        from scripts.ci.semantic_protocol import plan_execution_sha256
        bundle = load_migration_bundle(self.root)
        profile = next(row for row in bundle.profiles if row['appId'] == 'qq')
        changed = copy.deepcopy(self.report(profile))
        selected = changed['selection']['plan']
        selected['hooks'][0]['descriptor'] = selected['hooks'][0]['descriptor'].replace('->a(', '->changed(')
        digest = plan_execution_sha256(selected)
        changed['selection']['executionSha256'] = digest
        changed['recognizers'][0]['plans'][0].update(plan=selected, executionSha256=digest)
        with self.assertRaises(ValueError): compare_fixed_report(bundle, profile['historicalRecordId'], changed)

    def test_wrong_record_and_changed_legacy_catalog_pin_are_rejected(self):
        bundle = load_migration_bundle(self.root)
        with self.assertRaises(ValueError): compare_fixed_report(bundle, 'unregistered-record', self.report(bundle.profiles[0]))
        history = json.loads((self.root / 'compatibility/semantic-migration/history.json').read_text())
        reference = history['tables'][0]['records'][0]['catalog']['path']
        path = self.root / reference
        path.write_text(path.read_text().replace('wechat_tablet_login_001', 'changed'))
        with self.assertRaises(ValueError): load_migration_bundle(self.root)

    def test_catalog_reference_cannot_escape_repository(self):
        path = self.root / 'compatibility/semantic-migration/history.json'
        history = json.loads(path.read_text())
        history['tables'][0]['records'][0]['catalog']['path'] = '../outside.json'
        path.write_text(json.dumps(history))
        with self.assertRaises(ValueError): load_migration_bundle(self.root)


if __name__ == '__main__': unittest.main()
