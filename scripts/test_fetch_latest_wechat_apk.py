import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.ci.app_policy import get_policy
from scripts.ci.fetch_latest_wechat_apk import download_candidate, remote_metadata, request_urlopen
from urllib.request import Request


class Response:
    status = 200

    def __init__(self, url, body=b'candidate-apk'):
        self.url = url
        self.headers = {'Content-Length': str(len(body)), 'ETag': 'test-etag'}
        self.body = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.body.read(size)


class AppScopedDownloadTest(unittest.TestCase):
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
