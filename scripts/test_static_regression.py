import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.ci.static_regression import (cached_historical_apk, check_apk, ensure_apk,
                                          main)


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
                from scripts.ci.static_regression import HistoricalApkUnavailable
                record['source']['apkFiles'][1]['sha256'] = 'd' * 64
                with self.assertRaisesRegex(HistoricalApkUnavailable, 'base/split APK hashes'):
                    ensure_apk(record, root / 'cache', 'wechat')
                record['source']['apkFiles'][1]['sha256'] = hashes[1]
            files = record_apk_inputs(record, root / 'cache', base)
            self.assertEqual([b'base', b'split'], [path.read_bytes() for path in files])
            files[1].write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'hash'):
                record_apk_inputs(record, root / 'cache', base)

    def test_cached_historical_apk_is_cache_only_and_verifies_base_and_splits(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            payloads = (b'cached base', b'cached split one', b'cached split two')
            hashes = [hashlib.sha256(payload).hexdigest() for payload in payloads]
            for digest, payload in zip(hashes, payloads):
                (cache / f'{digest}.apk').write_bytes(payload)
            record = {
                'identity': {'packageName': 'com.tencent.mm', 'apkSha256': hashes[0]},
                'source': {'url': 'https://dldir1v6.qq.com/weixin/android/fixed.apk',
                           'apkFiles': [{'path': f'{index}.apk',
                                         'role': 'base' if index == 0 else 'split',
                                         'sha256': digest}
                                        for index, digest in enumerate(hashes)]},
            }
            with patch('scripts.ci.static_regression.remote_metadata') as metadata, \
                 patch('scripts.ci.static_regression.download_candidate') as download:
                self.assertEqual(cache / f'{hashes[0]}.apk',
                                 cached_historical_apk(record, cache, 'wechat'))
                metadata.assert_not_called()
                download.assert_not_called()

                (cache / f'{hashes[2]}.apk').write_bytes(b'corrupt split')
                self.assertIsNone(cached_historical_apk(record, cache, 'wechat'))
                metadata.assert_not_called()
                download.assert_not_called()

    def test_cached_historical_apk_rejects_untrusted_package_and_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            record = {'identity': {'packageName': 'com.example.other', 'apkSha256': 'a' * 64},
                      'source': {'url': 'https://example.invalid/apk'}}
            with self.assertRaisesRegex(ValueError, 'package'):
                cached_historical_apk(record, cache, 'wechat')
            record['identity']['packageName'] = 'com.tencent.mm'
            record['identity']['apkSha256'] = 'not-a-digest'
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                cached_historical_apk(record, cache, 'wechat')

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
                     patch('scripts.ci.static_regression.run_grouped_cli') as regression, \
                     self.assertRaises(SystemExit):
                    main()
                regression.assert_not_called()

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
