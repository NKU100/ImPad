import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.ci.discover_latest_wechat import discover_from_html, expected_sha256_for_url, validate_official_apk_url
from scripts.ci.fetch_latest_wechat_apk import cache_matches


class DiscoverLatestWechatTest(unittest.TestCase):
    def test_real_app_baselines_supply_registered_source_digests_without_catalog_loading(self):
        from scripts.ci.semantic_regression_baseline import load_semantic_regression_baseline

        for app_id in ('wechat', 'qq', 'wecom'):
            baseline = load_semantic_regression_baseline(Path.cwd(), app_id)
            self.assertEqual(4, len(baseline.migration_bundle.profiles))
            case = next(case for case in baseline.cases
                        if case.get('source', {}).get('url'))
            with patch('scripts.ci.acceptance.load_rule_catalog',
                       side_effect=AssertionError('catalog loaded')):
                self.assertEqual(case['identity']['apkSha256'].lower(),
                                 expected_sha256_for_url(app_id, case['source']['url']))

    def test_source_case_lookup_uses_exact_url_and_rejects_ambiguous_or_missing_digest(self):
        url = 'https://dldir1v6.qq.com/weixin/android/repack.apk'
        cases = [
            {'kind': 'MIGRATION', 'source': {'url': url},
             'identity': {'apkSha256': 'a' * 64}},
            {'kind': 'ACCEPTED_V2', 'source': {'url': url + '?variant=1'},
             'identity': {'apkSha256': 'c' * 64}},
        ]
        baseline = SimpleNamespace(cases=tuple(cases), assert_unchanged=lambda: None)
        with patch('scripts.ci.semantic_regression_baseline.load_semantic_regression_baseline',
                   return_value=baseline):
            self.assertEqual('a' * 64, expected_sha256_for_url('wechat', url, Path.cwd()))
            self.assertEqual('c' * 64,
                             expected_sha256_for_url('wechat', url + '?variant=1', Path.cwd()))
            cases.append({'kind': 'ACCEPTED_V2', 'source': {'url': url},
                          'identity': {'apkSha256': 'b' * 64}})
            baseline.cases = tuple(cases)
            self.assertEqual('', expected_sha256_for_url('wechat', url, Path.cwd()))
            self.assertEqual('', expected_sha256_for_url('wechat', url + '/other', Path.cwd()))

    def test_source_case_lookup_rejects_invalid_digest(self):
        url = 'https://dldir1v6.qq.com/weixin/android/repack.apk'
        baseline = SimpleNamespace(
            cases=({'kind': 'ACCEPTED_V2', 'source': {'url': url},
                    'identity': {'apkSha256': 'not-a-digest'}},),
            assert_unchanged=lambda: None,
        )
        with patch('scripts.ci.semantic_regression_baseline.load_semantic_regression_baseline',
                   return_value=baseline):
            with self.assertRaisesRegex(ValueError, 'invalid SHA-256 digest'):
                expected_sha256_for_url('wechat', url, Path.cwd())

    def test_source_case_lookup_propagates_invalid_baseline_errors(self):
        url = 'https://dldir1v6.qq.com/weixin/android/repack.apk'
        with patch('scripts.ci.semantic_regression_baseline.load_semantic_regression_baseline',
                   side_effect=ValueError('tampered baseline')):
            with self.assertRaisesRegex(ValueError, 'tampered baseline'):
                expected_sha256_for_url('wechat', url, Path.cwd())

    def test_source_case_lookup_rejects_baseline_mutation(self):
        url = 'https://dldir1v6.qq.com/weixin/android/repack.apk'
        baseline = SimpleNamespace(
            cases=({'kind': 'MIGRATION', 'source': {'url': url},
                    'identity': {'apkSha256': 'a' * 64}},),
            assert_unchanged=unittest.mock.Mock(side_effect=ValueError('baseline changed')),
        )
        with patch('scripts.ci.semantic_regression_baseline.load_semantic_regression_baseline',
                   return_value=baseline):
            with self.assertRaisesRegex(ValueError, 'baseline changed'):
                expected_sha256_for_url('wechat', url, Path.cwd())
        baseline.assert_unchanged.assert_called_once_with()

    def test_selects_unique_highest_official_arm64_build(self):
        html = """
        https://dldir1v6.qq.com/weixin/android/weixin8079android3200_0x28004f30_arm64.apk
        https://dldir1v6.qq.com/weixin/android/weixin8042android2460.apk
        https://dldir1v6.qq.com/weixin/android/weixin8034android2340_1.apk
        https://dldir1v6.qq.com/weixin/android/weixin8080android3220_0x28005010_arm64.apk
        https://dldir1v6.qq.com/weixin/android/weixin8080android3220_0x28005010.apk
        """

        candidate = discover_from_html(html)

        self.assertEqual(
            candidate.url,
            "https://dldir1v6.qq.com/weixin/android/weixin8080android3220_0x28005010_arm64.apk",
        )
        self.assertEqual(candidate.build_number, 8080)
        self.assertEqual(candidate.version_code, 3220)

    def test_rejects_multiple_urls_for_highest_build(self):
        html = """
        https://dldir1v6.qq.com/weixin/android/weixin8080android3220_0x28005010_arm64.apk
        https://dldir1v6.qq.com/weixin/android/weixin8080android3220_0x28005011_arm64.apk
        """

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            discover_from_html(html)

    def test_rejects_page_without_official_arm64_apk(self):
        html = "https://example.invalid/weixin/android/weixin8080android3220_arm64.apk"

        with self.assertRaisesRegex(ValueError, "no ARM64 APK"):
            discover_from_html(html)

    def test_rejects_non_official_download_hosts(self):
        with self.assertRaisesRegex(ValueError, "official Tencent CDN"):
            validate_official_apk_url("https://example.invalid/weixin/android/weixin8080android3220_arm64.apk")

    def test_reuses_cached_apk_only_when_source_metadata_and_hash_match(self):
        url = "https://dldir1v6.qq.com/weixin/android/weixin8079android3200_0x28004f30_arm64.apk"
        body = b"official apk bytes"
        digest = hashlib.sha256(body).hexdigest()
        with TemporaryDirectory() as temporary:
            cache = Path(temporary)
            (cache / f"{digest}.apk").write_bytes(body)
            (cache / "candidate.json").write_text(json.dumps({
                "url": url,
                "sha256": digest,
                "content_length": len(body),
                "last_modified": "Wed, 30 Sep 2026 13:03:57 GMT",
                "etag": "",
            }))

            self.assertTrue(cache_matches(
                cache,
                url,
                len(body),
                "Wed, 30 Sep 2026 13:03:57 GMT",
                "",
                digest,
            ))
            self.assertFalse(cache_matches(
                cache,
                url,
                len(body),
                "Thu, 01 Oct 2026 13:03:57 GMT",
                "",
                digest,
            ))

    def test_rejects_cached_apk_with_wrong_registered_digest(self):
        url = "https://dldir1v6.qq.com/weixin/android/weixin8079android3200_0x28004f30_arm64.apk"
        body = b"cached apk bytes"
        actual_digest = hashlib.sha256(body).hexdigest()
        with TemporaryDirectory() as temporary:
            cache = Path(temporary)
            (cache / f"{actual_digest}.apk").write_bytes(body)
            (cache / "candidate.json").write_text(json.dumps({
                "url": url,
                "sha256": actual_digest,
                "content_length": len(body),
                "last_modified": "Wed, 30 Sep 2026 13:03:57 GMT",
                "etag": "",
            }))

            self.assertFalse(cache_matches(
                cache,
                url,
                len(body),
                "Wed, 30 Sep 2026 13:03:57 GMT",
                "",
                "0" * 64,
            ))


if __name__ == "__main__":
    unittest.main()
