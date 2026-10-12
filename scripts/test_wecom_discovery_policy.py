import unittest
from unittest.mock import patch

from scripts.ci.app_policy import get_policy
from scripts.ci.apps.wecom import discovery_policy


class WeComDiscoveryPolicyTest(unittest.TestCase):
    def test_discovers_only_android_download_from_official_page(self):
        page = '''
            <a href="/">Home</a>
            <a href="/wework_admin/commdownload?platform=ios">iOS</a>
            <a href="/wework_admin/commdownload?platform=android&amp;from=wwindex">Android</a>
        '''
        apk_url = "https://dldir1.qq.com/wework/work_weixin/WeCom_android_5.0.11.81206_arm64_100001.apk"
        with patch.object(discovery_policy, "fetch_official_page", return_value=page), patch.object(
            discovery_policy, "resolve_download_url", return_value=apk_url
        ) as resolve:
            candidate = discovery_policy.discover(get_policy("wecom"))

        self.assertEqual(candidate.url, apk_url)
        self.assertEqual(candidate.filename, "WeCom_android_5.0.11.81206_arm64_100001.apk")
        self.assertEqual(candidate.build_number, 81206)
        self.assertEqual(candidate.version_code, 81206)
        resolve.assert_called_once_with(
            "https://work.weixin.qq.com/wework_admin/commdownload?platform=android&from=wwindex",
            get_policy("wecom").official_apk_prefix,
        )

    def test_rejects_missing_android_download_link(self):
        page = '<a href="/wework_admin/commdownload?platform=ios">iOS</a>'
        with patch.object(discovery_policy, "fetch_official_page", return_value=page):
            with self.assertRaisesRegex(ValueError, "no Android download link"):
                discovery_policy.discover(get_policy("wecom"))

    def test_rejects_ambiguous_android_download_links(self):
        page = '''
            <a href="/wework_admin/commdownload?platform=android">Android</a>
            <a href="/wework_admin/commdownload?platform=android&mirror=1">Android mirror</a>
        '''
        with patch.object(discovery_policy, "fetch_official_page", return_value=page):
            with self.assertRaisesRegex(ValueError, "ambiguous Android download links"):
                discovery_policy.discover(get_policy("wecom"))

    def test_rejects_redirect_outside_registered_tencent_apk_directory(self):
        with self.assertRaisesRegex(ValueError, "outside the official Tencent CDN APK directory"):
            discovery_policy.validate_resolved_url(
                "https://evil.example/WeCom_android_5.0.11.81206_arm64.apk",
                get_policy("wecom").official_apk_prefix,
            )


if __name__ == "__main__":
    unittest.main()
