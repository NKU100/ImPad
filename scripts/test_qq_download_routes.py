import unittest
import subprocess
from unittest.mock import patch
from urllib.request import Request


class QQDownloadRoutesTest(unittest.TestCase):
    def test_primary_dns_timeout_still_uses_alternative_routes(self):
        from scripts.ci.apps.qq.download_transport import QQDownloadTransport
        response = object()
        with patch('scripts.ci.apps.qq.download_transport.system_download_routes',
                   side_effect=subprocess.TimeoutExpired('dns', 8)), \
                patch('scripts.ci.apps.qq.download_transport.resolve_download_routes', return_value=[
                    {'source': 'regional', 'addresses': ['8.8.8.8']}]), \
                patch('scripts.ci.apps.qq.download_transport.edge_opener') as opener:
            opener.return_value.open.return_value = response
            self.assertIs(QQDownloadTransport().open(Request(
                'https://downv6.qq.com/qqweb/QQ_1/android_apk/a.apk'), 30), response)

    def test_alternative_resolution_has_a_process_deadline(self):
        from scripts.ci.apps.qq.download_routes import resolve_download_routes
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('resolver', 0.01)):
            result = resolve_download_routes(timeout=0.01)
        self.assertEqual(result[0]['addresses'], [])
        self.assertEqual(result[0]['error'], 'TimeoutExpired')

    def test_timeout_switches_to_regional_edge_and_reuses_success_for_get(self):
        from scripts.ci.apps.qq.download_transport import QQDownloadTransport
        response = object()
        attempts = []

        class Opener:
            def __init__(self, address):
                self.address = address
            def open(self, request, timeout):
                attempts.append((self.address, request.get_method(), timeout))
                if self.address == '1.1.1.1':
                    raise TimeoutError('unreachable primary')
                return response

        with patch('scripts.ci.apps.qq.download_transport.system_download_routes', return_value=['1.1.1.1']), \
                patch('scripts.ci.apps.qq.download_transport.resolve_download_routes', return_value=[
                    {'source': 'regional', 'addresses': ['8.8.8.8']}]), \
                patch('scripts.ci.apps.qq.download_transport.edge_opener', side_effect=lambda address, **kw: Opener(address)):
            transport = QQDownloadTransport()
            self.assertIs(transport.open(Request('https://downv6.qq.com/qqweb/QQ_1/android_apk/a.apk', method='HEAD'), 30), response)
            self.assertIs(transport.open(Request('https://downv6.qq.com/qqweb/QQ_1/android_apk/a.apk'), 120), response)
        self.assertEqual([(ip, method) for ip, method, _ in attempts],
                         [('1.1.1.1', 'HEAD'), ('8.8.8.8', 'HEAD'), ('8.8.8.8', 'GET')])
        self.assertTrue(all(timeout <= 6 for _, _, timeout in attempts))

    def test_http_forbidden_is_not_retried_as_a_network_failure(self):
        from scripts.ci.apps.qq.download_transport import QQDownloadTransport
        from urllib.error import HTTPError
        with patch('scripts.ci.apps.qq.download_transport.system_download_routes', return_value=['1.1.1.1']), \
                patch('scripts.ci.apps.qq.download_transport.edge_opener') as opener, \
                patch('scripts.ci.apps.qq.download_transport.resolve_download_routes') as resolve:
            opener.return_value.open.side_effect = HTTPError('https://downv6.qq.com', 403, 'Forbidden', {}, None)
            with self.assertRaises(HTTPError) as caught:
                QQDownloadTransport().open(Request('https://downv6.qq.com/qqweb/QQ_1/android_apk/a.apk'), 30)
            caught.exception.close()
            resolve.assert_not_called()
    def test_resolver_answers_keep_only_unique_public_ipv4(self):
        from scripts.ci.apps.qq.download_routes import public_ipv4_answers
        payload = {'Answer': [
            {'type': 5, 'data': 'edge.example'},
            {'type': 1, 'data': '1.1.1.1'}, {'type': 1, 'data': '1.1.1.1'},
            {'type': 1, 'data': '127.0.0.1'}, {'type': 1, 'data': '10.0.0.1'},
            {'type': 28, 'data': '2606:4700:4700::1111'},
            {'type': 1, 'data': 'not-an-address'},
        ]}
        self.assertEqual(public_ipv4_answers(payload), ['1.1.1.1'])

    def test_malformed_answers_do_not_become_routes(self):
        from scripts.ci.apps.qq.download_routes import public_ipv4_answers
        for payload in ({}, {'Answer': None}, {'Answer': ['invalid', {}]}):
            self.assertEqual(public_ipv4_answers(payload), [])


if __name__ == '__main__':
    unittest.main()
