import io
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.ci.app_policy import get_policy
from scripts.ci.fetch_latest_wechat_apk import (
    MIB, DownloadProgress, download_candidate, remote_metadata, request_urlopen,
)
from urllib.request import Request


class Response:
    status = 200

    def __init__(self, url, body=b'candidate-apk', failure=None):
        self.url = url
        self.headers = {'Content-Length': str(len(body)), 'ETag': 'test-etag'}
        self.body = io.BytesIO(body)
        self.failure = failure

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.body.read(size)

    def read1(self, size=-1):
        block = self.body.read(size)
        if not block and self.failure is not None:
            raise self.failure
        return block


class Clock:
    def __init__(self, *readings):
        self.readings = list(readings)

    def __call__(self):
        return self.readings.pop(0)


class DownloadProgressTest(unittest.TestCase):
    def test_reports_bar_speed_and_eta_only_after_each_interval(self):
        stream = io.StringIO()
        progress = DownloadProgress('qq.apk', 100 * MIB, stream=stream, clock=Clock(0, 5, 10, 15, 20, 25), interval=10)
        progress.report(0, progress.started)
        for received in (10, 25, 40, 50):
            progress.update(received * MIB)
        progress.finish(100 * MIB)
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith('Downloading qq.apk: [------------------------------]   0.0%'))
        self.assertIn('[#######-----------------------]  25.0% 25.0/100.0 MiB 2.5 MiB/s elapsed 10s ETA 30s', lines[1])
        self.assertIn('[###############---------------]  50.0% 50.0/100.0 MiB 2.5 MiB/s elapsed 20s ETA 20s', lines[2])
        self.assertIn('[##############################] 100.0% 100.0/100.0 MiB 4.0 MiB/s elapsed 25s done', lines[3])

    def test_unknown_length_reports_transferred_bytes_without_bar(self):
        stream = io.StringIO()
        progress = DownloadProgress('mirror.xapk', None, stream=stream, clock=Clock(0, 4))
        progress.finish(8 * MIB, 'stopped: TimeoutError')
        self.assertEqual(stream.getvalue().strip(),
                         'Downloading mirror.xapk: 8.0 MiB 2.0 MiB/s elapsed 4s stopped: TimeoutError')


class AppScopedDownloadTest(unittest.TestCase):
    def test_qq_early_eof_retries_without_accepting_a_truncated_apk(self):
        url = 'https://downv6.qq.com/qqweb/QQ_1/android_apk/test.apk'
        truncated = Response(url, body=b'partial')
        truncated.headers['Content-Length'] = '100'
        complete = Response(url, body=b'complete-apk')
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {'QQ_APK_EDGE_FALLBACK': '1'}), \
                patch('scripts.ci.apps.qq.download_transport.getproxies', return_value={}), \
                patch('scripts.ci.fetch_latest_wechat_apk.request_urlopen', side_effect=[truncated, complete]), \
                patch('scripts.ci.apps.qq.download_transport.transport.reject_preferred'), \
                patch('sys.stderr', io.StringIO()):
            path, _, _, _ = download_candidate(url, Path(temporary), '', {}, get_policy('qq'))
            self.assertEqual(path.read_bytes(), b'complete-apk')

    def test_qq_partial_transfer_is_discarded_before_retrying_another_edge(self):
        url = 'https://downv6.qq.com/qqweb/QQ_1/android_apk/test.apk'
        interrupted = Response(url, body=b'partial', failure=TimeoutError('read stalled'))
        interrupted.headers['Content-Length'] = '100'
        completed = Response(url, body=b'complete-apk')
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {'QQ_APK_EDGE_FALLBACK': '1'}), \
                patch('scripts.ci.apps.qq.download_transport.getproxies', return_value={}), \
                patch('scripts.ci.fetch_latest_wechat_apk.request_urlopen', side_effect=[interrupted, completed]), \
                patch('scripts.ci.apps.qq.download_transport.transport.reject_preferred') as reject, \
                patch('sys.stderr', io.StringIO()):
            path, digest, reused, _ = download_candidate(url, Path(temporary), '', {}, get_policy('qq'))
            self.assertEqual(path.read_bytes(), b'complete-apk')
            self.assertFalse(reused)
            self.assertEqual(len(digest), 64)
            self.assertFalse(list(Path(temporary).glob('.candidate-download-*')))
            reject.assert_called_once()

    def setUp(self):
        self.prefix = 'https://apk.synthetic.invalid/official/'
        self.policy = replace(get_policy('wechat'), official_apk_prefix=self.prefix)
        self.url = self.prefix + 'candidate.apk'

    def test_policy_official_prefix_accepts_synthetic_download_source(self):
        response = Response(self.url)
        with tempfile.TemporaryDirectory() as temporary, patch(
            'scripts.ci.fetch_latest_wechat_apk.urlopen', return_value=response
        ):
            apk, digest, reused, _ = download_candidate(
                self.url, Path(temporary), '', {}, policy=self.policy
            )
            self.assertTrue(apk.is_file())
            self.assertEqual(b'candidate-apk', apk.read_bytes())
            self.assertFalse(reused)
            self.assertEqual(64, len(digest))

    def test_download_reports_start_and_completion_without_query_string(self):
        url = 'https://mirror.synthetic.invalid/candidate.apk?token=secret'
        with tempfile.TemporaryDirectory() as temporary, patch(
            'scripts.ci.fetch_latest_wechat_apk.request_urlopen', return_value=Response(url)
        ), patch('scripts.ci.download_url.socket.getaddrinfo',
                 return_value=[(None, None, None, None, ('93.184.216.34', 443))]), \
                patch('sys.stderr', io.StringIO()) as log:
            download_candidate(url, Path(temporary), '', {}, policy=self.policy, allow_external=True)
        lines = log.getvalue().splitlines()
        self.assertTrue(lines[0].startswith('Downloading candidate.apk: [-'))
        self.assertIn('100.0%', lines[-1])
        self.assertTrue(lines[-1].endswith(' done'))
        self.assertNotIn('secret', log.getvalue())

    def test_interrupted_download_reports_received_bytes_and_error(self):
        response = Response(self.url, body=b'partial', failure=TimeoutError('The read operation timed out'))
        response.headers['Content-Length'] = '14'
        with tempfile.TemporaryDirectory() as temporary, patch(
            'scripts.ci.fetch_latest_wechat_apk.urlopen', return_value=response
        ), patch('sys.stderr', io.StringIO()) as log:
            with self.assertRaises(TimeoutError):
                download_candidate(self.url, Path(temporary), '', {}, policy=self.policy)
            self.assertEqual([], [path.name for path in Path(temporary).iterdir()])
        last = log.getvalue().splitlines()[-1]
        self.assertIn(' 50.0% 0.0/0.0 MiB', last)
        self.assertTrue(last.endswith('stopped: TimeoutError'))

    def test_policy_official_prefix_rejects_external_redirect(self):
        response = Response('https://outside.invalid/candidate.apk')
        with patch('scripts.ci.fetch_latest_wechat_apk.urlopen', return_value=response):
            with self.assertRaisesRegex(ValueError, 'redirected outside'):
                remote_metadata(self.url, policy=self.policy)

    def test_allow_external_accepts_synthetic_url_and_keeps_content_hash_cache(self):
        url = 'https://mirror.synthetic.invalid/file.xapk?token=x'
        response = Response(url)
        with tempfile.TemporaryDirectory() as temporary, patch(
            'scripts.ci.fetch_latest_wechat_apk.request_urlopen', return_value=response
        ), patch('scripts.ci.download_url.socket.getaddrinfo', return_value=[(None, None, None, None, ('93.184.216.34', 443))]):
            path, digest, reused, _ = download_candidate(
                url, Path(temporary), '', {}, self.policy, allow_external=True
            )
            self.assertEqual(f'{digest}.apk', path.name)
            self.assertFalse(reused)

    def test_default_remote_metadata_stays_strict(self):
        with patch('scripts.ci.fetch_latest_wechat_apk.urlopen') as open_url:
            with self.assertRaises(ValueError):
                remote_metadata('https://outside.invalid/file.apk', self.policy)
            open_url.assert_not_called()

    def test_external_initial_request_rejects_private_dns_before_open(self):
        request = Request('https://private.example/file.xapk')
        with patch('scripts.ci.download_url.socket.getaddrinfo', return_value=[(None, None, None, None, ('192.168.1.10', 443))]), \
                patch('scripts.ci.fetch_latest_wechat_apk.build_opener') as build_opener:
            with self.assertRaisesRegex(ValueError, 'public'):
                request_urlopen(request, timeout=30, allow_external=True)
            build_opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
