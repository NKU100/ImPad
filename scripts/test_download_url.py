import unittest
from unittest.mock import patch
from urllib.request import Request

from scripts.ci.download_url import SafeRedirectHandler, validate_download_target, validate_download_url


class DownloadUrlTest(unittest.TestCase):
    def test_allows_public_https_query_and_rejects_unsafe_components(self):
        validate_download_url('https://downloads.example/file.xapk?token=abc')
        for url in ('http://downloads.example/a', 'https://user@downloads.example/a',
                    'https://downloads.example/a#frag', 'https://localhost/a',
                    'https://127.0.0.1/a', 'https://downloads.example:99999/a',
                    'https://bad\\host/a', 'https://downloads.example/a\n'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_download_url(url)

    def test_rejects_dns_private_addresses(self):
        with patch('scripts.ci.download_url.socket.getaddrinfo', return_value=[(None, None, None, None, ('10.0.0.8', 443))]):
            with self.assertRaisesRegex(ValueError, 'public'):
                validate_download_target('https://downloads.example/file')

    def test_redirect_is_validated_before_request(self):
        handler = SafeRedirectHandler()
        request = Request('https://downloads.example/start')
        with patch('scripts.ci.download_url.validate_download_target') as validate, \
                patch('urllib.request.HTTPRedirectHandler.redirect_request', return_value='followed'):
            result = handler.redirect_request(None, request, 302, 'Found', {}, 'https://downloads.example/next')
        validate.assert_called_once_with('https://downloads.example/next')
        self.assertEqual('followed', result)

    def test_redirect_rejects_downgrade_before_following(self):
        handler = SafeRedirectHandler()
        request = Request('https://downloads.example/start')
        with self.assertRaises(ValueError):
            handler.redirect_request(None, request, 302, 'Found', {}, 'http://localhost/secret')

    def test_redirect_rejects_private_dns_before_following(self):
        handler = SafeRedirectHandler()
        request = Request('https://downloads.example/start')
        with patch('scripts.ci.download_url.socket.getaddrinfo', return_value=[(None, None, None, None, ('10.0.0.8', 443))]):
            with self.assertRaisesRegex(ValueError, 'public'):
                handler.redirect_request(None, request, 302, 'Found', {}, 'https://private.example/next')


if __name__ == '__main__':
    unittest.main()
