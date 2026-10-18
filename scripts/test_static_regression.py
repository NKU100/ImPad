import hashlib
from http.client import HTTPResponse
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.ci.static_regression import registered_window, ensure_apk, check_apk, main


class StaticRegressionTest(unittest.TestCase):
    def test_cli_requires_an_app_or_all_apps_instead_of_defaulting_to_wechat(self):
        with tempfile.TemporaryDirectory() as root, patch(
            'sys.argv', ['static_regression', '--reports', str(Path(root) / 'reports')]
        ), patch('scripts.ci.static_regression.run_regression') as run_regression:
            with self.assertRaises(SystemExit) as failure:
                main()

        self.assertEqual(failure.exception.code, 2)
        run_regression.assert_not_called()

    def test_truncated_http_response_records_failure_and_checks_remaining_apps(self):
        class Socket:
            def makefile(self, mode):
                return io.BytesIO(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n8\r\nabc')

        def fetch_apk(profile, cache, app_id):
            if app_id == 'qq':
                response = HTTPResponse(Socket())
                response.begin()
                response.read()
            return Path('fixture.apk')

        with tempfile.TemporaryDirectory() as root:
            reports = Path(root) / 'reports'
            with patch('sys.argv', ['static_regression', '--all-apps', '--reports', str(reports)]), \
                    patch('scripts.ci.static_regression.ensure_apk', side_effect=fetch_apk), \
                    patch('scripts.ci.static_regression.check_apk'):
                with self.assertRaises(SystemExit):
                    main()
            rows = json.loads((reports / 'results.json').read_text())
            self.assertEqual({'wechat', 'qq', 'wecom'}, {row['appId'] for row in rows})
            self.assertTrue(all(row['status'] == 'FAILED' for row in rows if row['appId'] == 'qq'))
            self.assertTrue(all(row['status'] == 'PASSED' for row in rows if row['appId'] == 'wecom'))
            self.assertTrue((reports / 'qq/results.json').is_file())

    def test_all_apps_write_separate_reports_and_combined_results(self):
        with tempfile.TemporaryDirectory() as root:
            reports = Path(root) / 'reports'

            def run_checker(repository, targets, apk, digest, log, **kwargs):
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(kwargs['app_id'])

            with patch('sys.argv', ['static_regression', '--all-apps', '--reports', str(reports)]), \
                    patch('scripts.ci.static_regression.ensure_apk', return_value=Path('fixture.apk')), \
                    patch('scripts.ci.static_regression.check_apk', side_effect=run_checker):
                main()
            combined = json.loads((reports / 'results.json').read_text())
            self.assertEqual({'wechat', 'qq', 'wecom'}, {row['appId'] for row in combined})
            for app_id in ['wechat', 'qq', 'wecom']:
                rows = json.loads((reports / app_id / 'results.json').read_text())
                self.assertGreater(len(rows), 0)
                self.assertTrue(all(row['appId'] == app_id and row['status'] == 'PASSED' for row in rows))
                self.assertEqual(len(rows), len(list((reports / app_id).glob('*.log'))))

    def test_all_apps_continue_after_one_app_fails_and_exit_unsuccessfully(self):
        with tempfile.TemporaryDirectory() as root:
            reports = Path(root) / 'reports'

            def run_checker(repository, targets, apk, digest, log, **kwargs):
                if kwargs['app_id'] == 'qq':
                    raise ValueError('QQ regression failed')

            with patch('sys.argv', ['static_regression', '--all-apps', '--reports', str(reports)]), \
                    patch('scripts.ci.static_regression.ensure_apk', return_value=Path('fixture.apk')), \
                    patch('scripts.ci.static_regression.check_apk', side_effect=run_checker):
                with self.assertRaises(SystemExit):
                    main()
            combined = json.loads((reports / 'results.json').read_text())
            self.assertEqual({'wechat', 'qq', 'wecom'}, {row['appId'] for row in combined})
            self.assertTrue(all(row['status'] == 'FAILED' for row in combined if row['appId'] == 'qq'))
            self.assertTrue(all(row['status'] == 'PASSED' for row in combined if row['appId'] == 'wecom'))

    def test_window_uses_three_highest_supported_builds(self):
        profiles = [{'identity': {'packageName': 'com.tencent.mm', 'abi': 'arm64-v8a', 'versionCode': n,
                                  'versionName': f'8.0.{n}', 'apkSha256': f'{n:064x}',
                                  'signerSha256': '0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c'},
                     'sourceUrl': f'https://dldir1v6.qq.com/weixin/android/test{n}.apk',
                     'hooks': [{'id': 'tablet'}, {'id': 'login'}], 'verificationStatus': status}
                    for n, status in [(1, 'runtime-verified-local'), (4, 'static-verified'),
                                      (2, 'runtime-verified-hosted'), (3, 'runtime-verified-local'),
                                      (5, 'runtime-verified-hosted')]]
        self.assertEqual([5, 3, 2], [p['identity']['versionCode'] for p in registered_window(profiles)])

    def test_window_keeps_all_supported_variants_from_three_recent_version_codes(self):
        profiles = [{'identity': {'packageName': 'com.tencent.mm', 'abi': 'arm64-v8a', 'versionCode': code,
                                  'versionName': name, 'apkSha256': digest,
                                  'signerSha256': '0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c'},
                     'sourceUrl': f'https://dldir1v6.qq.com/weixin/android/{name}.apk',
                     'hooks': [{'id': 'tablet'}, {'id': 'login'}],
                     'verificationStatus': 'runtime-verified-local'}
                    for code, name, digest in [(3, 'old', '3' * 64), (4, 'recent', '4' * 64),
                                               (5, 'latest', '5' * 64), (5, 'latest-repack', 'a' * 64)]]
        self.assertEqual([5, 5, 4, 3], [p['identity']['versionCode'] for p in registered_window(profiles)])

    def test_cache_is_rehashed_and_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            digest = hashlib.sha256(b'apk').hexdigest()
            profile = {'identity': {'packageName': 'com.tencent.mm', 'abi': 'arm64-v8a', 'apkSha256': digest}, 'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/test.apk'}
            path = Path(root) / (digest + '.apk')
            path.write_bytes(b'apk')
            self.assertEqual(path, ensure_apk(profile, Path(root)))
            path.write_bytes(b'bad')
            with patch('scripts.ci.static_regression.download_candidate', return_value=(path, digest, False, {})), patch('scripts.ci.static_regression.remote_metadata', return_value={}):
                with self.assertRaises(ValueError):
                    ensure_apk(profile, Path(root))

    def test_checker_rejects_changed_apk_before_running_gradle(self):
        with tempfile.TemporaryDirectory() as root:
            apk = Path(root) / 'wechat.apk'
            apk.write_bytes(b'bad')
            with patch('scripts.ci.static_regression.subprocess.run') as run:
                with self.assertRaises(ValueError):
                    check_apk(Path(root), Path('targets.json'), apk, '0' * 64, Path(root) / 'log')
                run.assert_not_called()
