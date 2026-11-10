import unittest


class QQDownloadRoutesTest(unittest.TestCase):
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
