import unittest
from scripts.ci.apps.qq.download_policy import valid_download_redirect

SOURCE = 'https://downv6.qq.com/qqweb/QQ_1/android_apk/9.3.70.41925_2a98ecf56e5ef04b.apk'
REDIRECT = ('https://797d79bd3230597f2f6472dfbaeefb14.rdt.tfogc.com/downv6.qq.com'
            '/qqweb/QQ_1/android_apk/9.3.70.41925_2a98ecf56e5ef04b.apk'
            '?mkey=signature&cip=address&proto=https&tx_domain=down.qq.com'
            '&tx_path=%2Fqqweb%2F&tx_id=id')


class QqDownloadPolicyTest(unittest.TestCase):
    def test_official_qq_signed_redirect_preserves_exact_source(self):
        self.assertTrue(valid_download_redirect(REDIRECT, SOURCE))
        delivery = REDIRECT.replace('.rdt.tfogc.com/', '.rdt.tfogc.com:49156/') + '&access_type='
        self.assertTrue(valid_download_redirect(delivery, SOURCE))

    def test_redirect_rejects_wrong_host_path_scheme_or_origin(self):
        for url, source in [(REDIRECT.replace('.rdt.tfogc.com', '.rdt.tfogc.com.evil.org'), SOURCE),
                            (REDIRECT.replace('https://', 'http://'), SOURCE),
                            (REDIRECT.replace('9.3.70', '9.3.75'), SOURCE),
                            (REDIRECT + '&tx_domain=evil.org', SOURCE),
                            (REDIRECT, 'https://example.org/qq.apk'), (REDIRECT, None),
                            (REDIRECT.replace('.rdt.tfogc.com/', '.rdt.tfogc.com:444/'), SOURCE),
                            (REDIRECT + '&access_type=other', SOURCE)]:
            with self.subTest(url=url, source=source):
                self.assertFalse(valid_download_redirect(url, source))

    def test_shared_downloader_allows_route_only_for_qq_and_verified_source(self):
        from scripts.ci.app_policy import get_policy
        from scripts.ci.fetch_latest_wechat_apk import final_url_is_official
        self.assertTrue(final_url_is_official(REDIRECT, get_policy('qq'), SOURCE))
        self.assertFalse(final_url_is_official(REDIRECT, get_policy('qq')))
        self.assertFalse(final_url_is_official(REDIRECT, get_policy('wechat'), SOURCE))
        self.assertFalse(final_url_is_official(REDIRECT, get_policy('wecom'), SOURCE))


if __name__ == '__main__':
    unittest.main()
