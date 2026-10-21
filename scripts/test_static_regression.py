import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.ci.acceptance import AcceptanceSnapshot
from scripts.ci.static_regression import check_apk, ensure_apk, main, run_regression


class StaticRegressionTest(unittest.TestCase):
    def test_checker_source_has_only_the_shared_rules_entrypoint(self):
        source = Path('scripts/ci/static_regression.py').read_text()
        self.assertIn("'check-rules'", source)
        for removed_surface in ('resolution_mode', 'targets_path', 'resolution-modes',
                                'initial_migration', '--initial-migration', '--targets',
                                'registered_window', 'expectedDescriptor'):
            with self.subTest(removed_surface=removed_surface):
                self.assertNotIn(removed_surface, source)

    def test_cli_requires_explicit_app_and_rejects_migration_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            reports = Path(temporary) / 'reports'
            for arguments in (['static_regression', '--reports', str(reports)],
                              ['static_regression', '--app-id', 'wechat', '--initial-migration']):
                with self.subTest(arguments=arguments), patch('sys.argv', arguments), \
                     patch('scripts.ci.static_regression.run_regression') as regression, \
                     self.assertRaises(SystemExit):
                    main()
                regression.assert_not_called()

    def test_missing_or_empty_acceptance_fails_without_static_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = AcceptanceSnapshot(root / 'compatibility/qq/acceptance.json',
                                          b'{}', 'a' * 64, b'[]', False)
            with patch('scripts.ci.static_regression.read_acceptance_snapshot', return_value=snapshot), \
                 patch('scripts.ci.static_regression.select_representatives', return_value=[]), \
                 patch('scripts.ci.static_regression.ensure_apk') as download:
                rows = run_regression('qq', root / 'reports', repository=root)
            self.assertEqual('FAILED', rows[0]['status'])
            self.assertIn('representative', rows[0]['error'])
            download.assert_not_called()
            self.assertEqual(rows, json.loads((root / 'reports/results.json').read_text()))

    def test_passed_row_binds_identity_plan_record_and_acceptance_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            acceptance_path = root / 'compatibility/qq/acceptance.json'
            raw = b'{"schemaVersion":1,"appId":"qq","records":["fixed"]}\n'
            snapshot = AcceptanceSnapshot(acceptance_path, raw, hashlib.sha256(raw).hexdigest(),
                                          b'[]', False)
            identity = {'packageName': 'com.tencent.mobileqq', 'abi': 'arm64-v8a',
                        'versionCode': 14378, 'apkSha256': 'a' * 64}
            plan = {'selection': 'UNIQUE', 'ruleId': 'qq_device_type_001',
                    'strategyId': 'qq_tablet_001', 'targets': {'device_type': {}}}
            record = {'appId': 'qq', 'recordId': 'qq-first', 'identity': identity,
                      'expectedPlan': plan, 'source': {'url': 'https://downv6.qq.com/qqweb/QQ_1/android_apk/test.apk'}}
            apk = root / 'fixed.apk'
            apk.write_bytes(b'official apk')

            def check(repository, candidate_apk, digest, log, app_id):
                log.parent.mkdir(parents=True, exist_ok=True)
                log.with_suffix('.json').write_text('{}')
                return {'identity': identity, 'selection': {
                    'status': 'SELECTED', 'selectedRuleId': plan['ruleId'],
                    'strategyId': plan['strategyId'], 'targets': plan['targets']}}

            with patch('scripts.ci.static_regression.read_acceptance_snapshot', return_value=snapshot), \
                 patch('scripts.ci.static_regression.select_representatives', return_value=[record]), \
                 patch('scripts.ci.static_regression.ensure_apk', return_value=apk), \
                 patch('scripts.ci.static_regression.check_apk', side_effect=check) as run_checker:
                rows = run_regression('qq', root / 'reports', repository=root)

            self.assertEqual('PASSED', rows[0]['status'])
            self.assertEqual('qq-first', rows[0]['recordId'])
            self.assertEqual(identity, rows[0]['identity'])
            self.assertEqual(plan, rows[0]['expectedPlan'])
            self.assertEqual(snapshot.sha256, rows[0]['acceptanceSnapshotSha256'])
            self.assertIs(rows[0]['initialMigration'], False)
            self.assertEqual('qq', run_checker.call_args.kwargs['app_id'])

    def test_failed_historical_row_binds_snapshot_as_non_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = b'{"schemaVersion":1,"appId":"qq","records":["fixed"]}\n'
            snapshot = AcceptanceSnapshot(root / 'compatibility/qq/acceptance.json', raw,
                                          hashlib.sha256(raw).hexdigest(), b'[]', False)
            identity = {'packageName': 'com.tencent.mobileqq', 'abi': 'arm64-v8a',
                        'versionCode': 14378, 'apkSha256': 'a' * 64}
            record = {'appId': 'qq', 'recordId': 'qq-first', 'identity': identity,
                      'expectedPlan': {'selection': 'UNIQUE'}}
            apk = root / 'fixed.apk'
            apk.write_bytes(b'official apk')
            with patch('scripts.ci.static_regression.read_acceptance_snapshot', return_value=snapshot), \
                 patch('scripts.ci.static_regression.select_representatives', return_value=[record]), \
                 patch('scripts.ci.static_regression.ensure_apk', return_value=apk), \
                 patch('scripts.ci.static_regression.check_apk', side_effect=ValueError('checker mismatch')):
                rows = run_regression('qq', root / 'reports', repository=root)
            self.assertEqual('FAILED', rows[0]['status'])
            self.assertEqual(snapshot.sha256, rows[0]['acceptanceSnapshotSha256'])
            self.assertIs(rows[0]['initialMigration'], False)
            self.assertEqual(rows, json.loads((root / 'reports/results.json').read_text()))

    def test_checker_rehashes_exact_apk_and_runs_check_rules_for_explicit_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'qq.apk'
            apk.write_bytes(b'exact candidate')
            digest = hashlib.sha256(apk.read_bytes()).hexdigest()
            log = root / 'checker.log'
            result_path = log.with_suffix('.json')
            result_path.write_text(json.dumps({'selection': {'status': 'SELECTED'}}))
            with patch('scripts.ci.static_regression.subprocess.run') as run:
                report = check_apk(root, apk, digest, log, app_id='qq')
            self.assertEqual('SELECTED', report['selection']['status'])
            command = run.call_args.args[0]
            self.assertIn(':compat-checker:run', command)
            self.assertTrue(command[-1].startswith('--args=check-rules'))
            self.assertIn('--app-id qq', command[-1])
            self.assertIn(str(apk.resolve()), command[-1])
            with patch('scripts.ci.static_regression.subprocess.run') as run:
                with self.assertRaisesRegex(ValueError, 'changed'):
                    check_apk(root, apk, '0' * 64, log, app_id='qq')
                run.assert_not_called()

    def test_fixed_acceptance_apk_cache_is_rehashed(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            payload = b'accepted apk'
            digest = hashlib.sha256(payload).hexdigest()
            apk = cache / f'{digest}.apk'
            apk.write_bytes(payload)
            record = {'identity': {'packageName': 'com.tencent.mm', 'apkSha256': digest},
                      'source': {'url': 'https://dldir1v6.qq.com/weixin/android/fixed.apk'}}
            self.assertEqual(apk, ensure_apk(record, cache, 'wechat'))
            apk.write_bytes(b'corrupt')
            with patch('scripts.ci.static_regression.remote_metadata', return_value={}), \
                 patch('scripts.ci.static_regression.download_candidate', return_value=(apk, digest, False, {})) as download:
                with self.assertRaises(ValueError):
                    ensure_apk(record, cache, 'wechat')
                download.assert_called_once()


if __name__ == '__main__':
    unittest.main()
