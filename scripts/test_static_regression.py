import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.test_semantic_migration import SemanticMigrationTest, ROOT
from scripts.ci.static_regression import check_apk, ensure_apk, main, run_regression


class StaticRegressionTest(unittest.TestCase):
    def test_regression_failure_evidence_distinguishes_matcher_from_execution(self):
        from scripts.ci.static_regression import classify_regression_failure
        from subprocess import TimeoutExpired
        from urllib.error import URLError
        cases = [
            ('download', URLError('network unavailable'), 'execution_failure'),
            ('checker', TimeoutExpired('gradle', 300), 'execution_failure'),
            ('checker_report', ValueError('checker report mismatch'), 'check_failure'),
            ('comparison', ValueError('semantic baseline differs'), 'check_failure'),
            ('setup', OSError('cannot read report'), 'execution_failure'),
        ]
        for stage, error, expected in cases:
            with self.subTest(stage=stage, error=type(error).__name__):
                self.assertEqual(expected, classify_regression_failure(stage, error))

    def test_fixed_xapk_restores_and_hash_checks_all_original_splits(self):
        import zipfile
        from scripts.ci.static_regression import record_apk_inputs
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'bundle.xapk'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('com.tencent.mm.apk', b'base')
                output.writestr('config.arm64_v8a.apk', b'split')
                output.writestr('manifest.json', '{}')
            hashes = [hashlib.sha256(payload).hexdigest() for payload in (b'base', b'split')]
            record = {'identity': {'packageName': 'com.tencent.mm', 'apkSha256': hashes[0]},
                      'source': {'type': 'signed-download', 'url': 'https://mirror.example/download?code=3182',
                                 'archiveSha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                                 'apkFiles': [{'path': f'{index}.apk', 'role': 'base' if index == 0 else 'split',
                                               'sha256': digest} for index, digest in enumerate(hashes)]}}
            with patch('scripts.ci.static_regression.remote_metadata', return_value={}), \
                 patch('scripts.ci.static_regression.download_candidate',
                       return_value=(archive, record['source']['archiveSha256'], False, {})):
                base = ensure_apk(record, root / 'cache', 'wechat')
            files = record_apk_inputs(record, root / 'cache', base)
            self.assertEqual([b'base', b'split'], [path.read_bytes() for path in files])
            files[1].write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'hash'):
                record_apk_inputs(record, root / 'cache', base)

    def test_explicit_repository_loads_catalog_and_checks_cache_outside_cwd(self):
        import shutil
        from scripts.ci.semantic_migration import load_migration_bundle
        helper = SemanticMigrationTest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository = root / 'candidate-checkout'
            shutil.copytree(ROOT / 'compatibility', repository / 'compatibility')
            bundle = load_migration_bundle(repository)
            profile = next(row for row in bundle.profiles if row['appId'] == 'qq')
            apk = root / 'fixed.apk'; apk.write_bytes(b'fixture')
            report = helper.report(profile)
            def check(repository, candidate_apk, digest, log, app_id, **kwargs):
                log.parent.mkdir(parents=True, exist_ok=True)
                log.with_suffix('.json').write_text(json.dumps(report))
                return report
            outside = root / 'outside'; outside.mkdir()
            original_cwd = Path.cwd()
            try:
                os.chdir(outside)
                with patch('scripts.ci.static_regression.ensure_apk', return_value=apk), \
                     patch('scripts.ci.static_regression.record_apk_inputs', return_value=[apk]), \
                     patch('scripts.ci.static_regression.check_apk', side_effect=check):
                    rows = run_regression('qq', root / 'reports', repository=repository)
            finally:
                os.chdir(original_cwd)
            self.assertEqual('PASSED', rows[0]['status'], rows)
            self.assertEqual(len([row for row in bundle.profiles if row['appId'] == 'qq']), len(rows))
            self.assertEqual(profile['historicalRecordId'], rows[0]['recordId'])

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
            with patch('scripts.ci.static_regression.load_migration_bundle', side_effect=ValueError('missing representative')), \
                 patch('scripts.ci.static_regression.ensure_apk') as download:
                rows = run_regression('qq', root / 'reports', repository=root)
            self.assertEqual('FAILED', rows[0]['status'])
            self.assertEqual('check_failure', rows[0]['failureKind'])
            self.assertIn('representative', rows[0]['error'])
            download.assert_not_called()
            self.assertEqual(rows, json.loads((root / 'reports/results.json').read_text()))

    def test_download_error_is_gray_evidence_and_matcher_mismatch_is_check_evidence(self):
        import shutil
        from urllib.error import URLError
        from scripts.ci.semantic_migration import load_migration_bundle
        from scripts.ci.semantic_regression_baseline import load_semantic_regression_baseline

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / 'compatibility', root / 'compatibility')
            bundle = load_migration_bundle(root)
            baseline = load_semantic_regression_baseline(root, 'qq', bundle)
            self.assertTrue(baseline.cases)

            with patch('scripts.ci.static_regression.ensure_apk', side_effect=URLError('network unavailable')):
                rows = run_regression('qq', root / 'download-report', repository=root)
            self.assertTrue(all(row.get('failureKind') == 'execution_failure' for row in rows), rows)

            apk = root / 'fixed.apk'
            apk.write_bytes(b'fixed candidate')
            with patch('scripts.ci.static_regression.ensure_apk', return_value=apk), \
                 patch('scripts.ci.static_regression.record_apk_inputs', return_value=[apk]), \
                 patch('scripts.ci.static_regression.check_apk', side_effect=
                       __import__('scripts.ci.static_regression', fromlist=['SemanticRegressionMismatch']).
                       SemanticRegressionMismatch('checker rejected plan')):
                rows = run_regression('qq', root / 'matcher-report', repository=root)
            self.assertTrue(all(row.get('failureKind') == 'check_failure' for row in rows), rows)

    def test_all_frozen_profiles_run_semantic_comparison_and_bind_migration_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            import shutil
            shutil.copytree(ROOT / 'compatibility', root / 'compatibility')
            migration = SemanticMigrationTest()
            from scripts.ci.semantic_migration import load_migration_bundle
            loaded = load_migration_bundle(root)
            self.assertEqual(4, len(loaded.profiles))
            for profile in loaded.profiles:
                app_id = profile['appId']
                record = next(row for row in loaded.historical_records if row['recordId'] == profile['historicalRecordId'])
                apk = root / (profile['profileId'] + '.apk')
                apk.write_bytes(b'fixed base')
                report = migration.report(profile)
                def check(repository, candidate_apk, digest, log, app_id, **kwargs):
                    chosen = next(row for row in loaded.profiles if row['identity']['apkSha256'] == digest)
                    checked = migration.report(chosen)
                    log.parent.mkdir(parents=True, exist_ok=True)
                    log.with_suffix('.json').write_text(json.dumps(checked))
                    return checked
                with patch('scripts.ci.static_regression.load_migration_bundle', return_value=loaded), \
                     patch('scripts.ci.static_regression.ensure_apk', return_value=apk) as ensure, \
                     patch('scripts.ci.static_regression.record_apk_inputs', return_value=[apk]), \
                     patch('scripts.ci.static_regression.check_apk', side_effect=check), \
                     patch('scripts.ci.static_regression.compare_baseline_report', wraps=__import__(
                         'scripts.ci.semantic_regression_baseline', fromlist=['compare_baseline_report']).compare_baseline_report) as compare:
                    rows = run_regression(app_id, root / ('reports-' + profile['profileId']), repository=root)
                self.assertTrue(all(row['status'] == 'PASSED' for row in rows), rows)
                passed = next(row for row in rows if row['profileId'] == profile['profileId'])
                self.assertEqual(profile['historicalRecordId'], passed['recordId'])
                self.assertEqual(profile['historicalAcceptanceSha256'], passed['acceptanceSnapshotSha256'])
                self.assertEqual(loaded.history_sha256, passed['migrationHistorySha256'])
                self.assertEqual(loaded.expectations_sha256, passed['migrationExpectationsSha256'])
                self.assertEqual(profile['profileId'], passed['profileId'])
                self.assertFalse(passed['initialMigration'])
                self.assertNotIn('newacceptance', json.dumps(passed).lower())
                self.assertTrue(all(call.args[0]['source']['url'] for call in ensure.call_args_list))
                self.assertIn(profile['historicalRecordId'], [call.args[1] for call in compare.call_args_list])

    def test_selected_legacy_or_wrong_frozen_target_fails_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            import shutil
            shutil.copytree(ROOT / 'compatibility', root / 'compatibility')
            from scripts.ci.semantic_migration import load_migration_bundle, compare_fixed_report
            bundle = load_migration_bundle(root)
            profile = next(row for row in bundle.profiles if row['appId'] == 'qq')
            helper = SemanticMigrationTest()
            report = helper.report(profile)
            with self.assertRaises(ValueError):
                compare_fixed_report(bundle, profile['historicalRecordId'], {'selection': {'status': 'SELECTED'}})
            report['selection']['plan']['hooks'][0]['descriptor'] += 'L'
            with self.assertRaises(ValueError):
                compare_fixed_report(bundle, profile['historicalRecordId'], report)

    def test_checker_rehashes_exact_apk_and_runs_check_rules_for_explicit_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'qq.apk'
            apk.write_bytes(b'exact candidate')
            digest = hashlib.sha256(apk.read_bytes()).hexdigest()
            log = root / 'checker.log'
            result_path = log.with_suffix('.json')
            from scripts.test_semantic_protocol import check_report
            result_path.write_text(json.dumps(check_report()))
            digest = 'b' * 64
            def fresh_success(command, **kwargs):
                result_path.write_text(json.dumps(check_report()))
                return __import__('subprocess').CompletedProcess(command, 0)
            with patch('scripts.ci.static_regression.subprocess.run', side_effect=fresh_success) as run:
                with patch('scripts.ci.static_regression.file_sha256', return_value=digest):
                    report = check_apk(root, apk, digest, log, app_id='qq')
            self.assertEqual('SELECTED', report['selection']['status'])
            command = run.call_args.args[0]
            self.assertIn(':compat-checker:run', command)
            self.assertTrue(command[-1].startswith('--args=check-rules'))
            self.assertIn('--app-id qq', command[-1])
            self.assertIn(str(apk.resolve()), command[-1])
            self.assertFalse(run.call_args.kwargs['check'])
            with patch('scripts.ci.static_regression.subprocess.run') as run:
                from scripts.ci.static_regression import StaticCheckerExecutionError
                with self.assertRaisesRegex(StaticCheckerExecutionError, 'changed'):
                    check_apk(root, apk, '0' * 64, log, app_id='qq')
                run.assert_not_called()

    def test_fresh_valid_nonzero_no_match_is_semantic_failure(self):
        import subprocess
        from scripts.ci.static_regression import SemanticRegressionMismatch
        from scripts.test_candidate_selection import CandidateSelectionTest
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'qq.apk'; apk.write_bytes(b'fixed apk')
            digest = hashlib.sha256(apk.read_bytes()).hexdigest()
            log = root / 'checker.log'
            report_path = log.with_suffix('.json')
            fresh = CandidateSelectionTest._semantic_report('qq', apk, 'NO_MATCH')

            def nonzero(command, **kwargs):
                report_path.write_text(json.dumps(fresh))
                return subprocess.CompletedProcess(command, 1)

            with patch('scripts.ci.static_regression.subprocess.run', side_effect=nonzero):
                with self.assertRaises(SemanticRegressionMismatch):
                    check_apk(root, apk, digest, log, app_id='qq')

    def test_checker_diagnostics_are_execution_failures(self):
        import subprocess
        from scripts.ci.static_regression import StaticCheckerExecutionError
        from scripts.test_candidate_selection import CandidateSelectionTest
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'qq.apk'
            apk.write_bytes(b'fixed apk')
            digest = hashlib.sha256(apk.read_bytes()).hexdigest()
            log = root / 'checker.log'
            for status in ('EVALUATION_ERROR', 'UNSUPPORTED_INPUT', 'INCOMPLETE'):
                with self.subTest(status=status):
                    fresh = CandidateSelectionTest._semantic_report('qq', apk, status)

                    def nonzero(command, **kwargs):
                        log.with_suffix('.json').write_text(json.dumps(fresh))
                        return subprocess.CompletedProcess(command, 1)

                    with patch('scripts.ci.static_regression.subprocess.run', side_effect=nonzero):
                        with self.assertRaises(StaticCheckerExecutionError):
                            check_apk(root, apk, digest, log, app_id='qq')

    def test_missing_stale_and_invalid_checker_reports_remain_execution_failures(self):
        import subprocess
        from scripts.ci.static_regression import classify_regression_failure
        from scripts.test_candidate_selection import CandidateSelectionTest
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'qq.apk'; apk.write_bytes(b'fixed apk')
            digest = hashlib.sha256(apk.read_bytes()).hexdigest()
            log = root / 'checker.log'
            report_path = log.with_suffix('.json')
            stale = CandidateSelectionTest._semantic_report('qq', apk, 'NO_MATCH')
            report_path.write_text(json.dumps(stale))

            def no_fresh_report(command, **kwargs):
                return subprocess.CompletedProcess(command, 1)

            with patch('scripts.ci.static_regression.subprocess.run', side_effect=no_fresh_report):
                with self.assertRaises(subprocess.CalledProcessError) as error:
                    check_apk(root, apk, digest, log, app_id='qq')
            self.assertEqual('execution_failure', classify_regression_failure('checker_execution', error.exception))
            self.assertFalse(report_path.exists())

            invalid_reports = [b'{broken',
                               json.dumps(dict(stale, appId='wecom')).encode(),
                               json.dumps(dict(stale, inputHashes=[{'path': str(apk), 'sha256': 'f' * 64}])).encode()]
            for invalid in invalid_reports:
                def invalid_fresh_report(command, **kwargs):
                    report_path.write_bytes(invalid)
                    return subprocess.CompletedProcess(command, 1)

                with patch('scripts.ci.static_regression.subprocess.run', side_effect=invalid_fresh_report):
                    with self.assertRaises(subprocess.CalledProcessError) as error:
                        check_apk(root, apk, digest, log, app_id='qq')
                self.assertEqual('execution_failure',
                                 classify_regression_failure('checker_execution', error.exception))

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
