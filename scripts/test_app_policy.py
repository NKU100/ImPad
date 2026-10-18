import unittest
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

from scripts.ci.app_policy import POLICIES, get_policy, policy_for_report, validate_targets
from scripts.ci.codex_adaptation import validate_paths


class AppPolicyTest(unittest.TestCase):
    def test_adaptation_rejects_checker_changes_for_every_app(self):
        for app_id in ('wechat', 'wecom', 'qq'):
            with self.subTest(app_id=app_id), self.assertRaisesRegex(ValueError, 'outside the allowed'):
                validate_paths(['compat-checker/src/main/kotlin/io/github/nku100/impad/checker/Main.kt'], app_id)

    def test_wechat_allowlist_uses_impad_package_paths(self):
        policy = get_policy('wechat')
        allowed = [
            'compatibility/wechat/targets.json',
            policy.app_source_prefix + 'WeChatAppAdapter.kt',
            'app/src/main/kotlin/io/github/nku100/impad/ImPadModule.kt',
        ]
        validate_paths(allowed, 'wechat')
        with self.assertRaisesRegex(ValueError, 'outside the allowed'):
            validate_paths(['app/src/main/kotlin/io/github/nku100/wechatpad/Legacy.kt'], 'wechat')

    def test_unknown_explicit_app_never_falls_back_to_wechat(self):
        with self.assertRaisesRegex(ValueError, "Unknown or unregistered appId"):
            get_policy("unregistered")
        with self.assertRaisesRegex(ValueError, "Unknown or unregistered appId"):
            policy_for_report({"identity": {"packageName": "com.tencent.mm"}}, "unregistered")

    def test_wecom_runtime_policy_is_separate_from_wechat(self):
        wechat = get_policy("wechat")
        wecom = get_policy("wecom")
        self.assertEqual(wecom.package_name, "com.tencent.wework")
        self.assertEqual(wecom.required_hook_ids, ("device_type", "app_pad", "service_pad"))
        self.assertEqual(wecom.runtime_baseline, "NO_TABLET_ENTRY")
        self.assertEqual(wecom.runtime_qr_page, "LoginQrCodeForAndroidPad")
        self.assertNotEqual(wecom.runtime_ui_strategy, wechat.runtime_ui_strategy)
        with self.assertRaisesRegex(ValueError, "package"):
            policy_for_report({"appId": "wecom", "identity": {"packageName": "com.tencent.mm"}}, "wecom")

    def test_wecom_policy_points_to_a_valid_target_manifest(self):
        policy = get_policy("wecom")
        targets_path = Path(policy.targets_path)
        self.assertTrue(targets_path.is_file(), policy.targets_path)
        self.assertTrue(Path(policy.adaptation_prompt).is_file(), policy.adaptation_prompt)
        targets = json.loads(targets_path.read_text(encoding="utf-8"))
        validate_targets(targets, "wecom")
        self.assertEqual(targets[0]["identity"]["versionName"], "5.0.8")
        self.assertEqual(targets[0]["identity"]["versionCode"], 70675)
        self.assertTrue(targets[0]["sourceUrl"].startswith(policy.official_apk_prefix))

    def test_test_only_second_policy_is_confined_to_its_own_paths(self):
        test_policy = replace(
            get_policy("wechat"), app_id="testapp", package_name="org.example.testapp",
            targets_path="compatibility/testapp/targets.json",
            app_source_prefix="app/src/main/kotlin/example/testapp/",
            shared_source_paths=(), artifact_prefix="test-only",
        )
        with patch.dict(POLICIES, {"testapp": test_policy}):
            validate_paths(["compatibility/testapp/targets.json", "app/src/main/kotlin/example/testapp/Adapter.kt"], "testapp")
            for path in ["compatibility/wechat/targets.json", "app/src/main/kotlin/io/github/nku100/impad/apps/wechat/Adapter.kt"]:
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "outside the allowed"):
                    validate_paths([path], "testapp")

    def test_target_manifest_requires_policy_package_hooks_and_abi(self):
        target = {
            "identity": {
                "packageName": "com.tencent.mm", "abi": "arm64-v8a", "versionCode": 1,
                "apkSha256": "a" * 64,
                "signerSha256": get_policy("wechat").signer_sha256,
            },
            "sourceUrl": "https://dldir1v6.qq.com/weixin/android/test.apk",
            "hooks": [{"id": "tablet"}, {"id": "login"}],
        }
        validate_targets([target], "wechat")
        target["identity"]["packageName"] = "org.example.testapp"
        with self.assertRaisesRegex(ValueError, "package"):
            validate_targets([target], "wechat")

    def test_target_manifest_uses_normalized_apk_digest_as_variant_identity(self):
        target = {
            "identity": {
                "packageName": "com.tencent.mm", "abi": "arm64-v8a", "versionCode": 1,
                "apkSha256": "a" * 64,
                "signerSha256": get_policy("wechat").signer_sha256,
            },
            "sourceUrl": "https://dldir1v6.qq.com/weixin/android/test.apk",
            "hooks": [{"id": "tablet"}, {"id": "login"}],
        }
        repack = {**target, "identity": {**target["identity"], "apkSha256": "B" * 64}}
        validate_targets([target, repack], "wechat")
        duplicate = {**target, "identity": {**target["identity"], "apkSha256": "A" * 64}}
        with self.assertRaisesRegex(ValueError, "(?i)duplicate.*profile"):
            validate_targets([target, duplicate], "wechat")


if __name__ == "__main__":
    unittest.main()
