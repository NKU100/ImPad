import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.ci.app_policy import POLICIES, get_policy, policy_for_report
from scripts.ci.codex_adaptation import validate_paths


class AppPolicyTest(unittest.TestCase):
    def test_registered_app_policies_have_no_legacy_mode_or_targets_path(self):
        self.assertEqual(set(POLICIES), {"wechat", "qq", "wecom"})
        for app_id in ("wechat", "qq", "wecom"):
            with self.subTest(app_id=app_id):
                policy = get_policy(app_id)
                self.assertFalse(hasattr(policy, "resolution_mode"))
                self.assertFalse(hasattr(policy, "targets_path"))
        with self.assertRaisesRegex(ValueError, "Unknown or unregistered appId"):
            get_policy(None)

    def test_checker_entrypoints_are_only_shared_rules_and_catalog(self):
        main = Path("compat-checker/src/main/kotlin/io/github/nku100/impad/checker/Main.kt").read_text()
        self.assertNotIn('"check" ->', main)
        self.assertNotIn('"analyze-candidate" ->', main)
        self.assertIn('"check-rules" ->', main)
        self.assertIn('"catalog" ->', main)

    def test_old_descriptor_result_model_is_not_part_of_the_rules_runtime(self):
        self.assertFalse(Path('compat-core/src/main/kotlin/io/github/nku100/impad/compat/CompatibilityResult.kt').exists())

    def test_adaptation_rejects_checker_changes_for_every_app(self):
        for app_id in ('wechat', 'wecom', 'qq'):
            with self.subTest(app_id=app_id), self.assertRaisesRegex(ValueError, 'outside the allowed'):
                validate_paths(['compat-checker/src/main/kotlin/io/github/nku100/impad/checker/Main.kt'], app_id)

    def test_wechat_allowlist_uses_impad_package_paths(self):
        policy = get_policy('wechat')
        allowed = [
            policy.app_source_prefix + 'WeChatAppAdapter.kt',
            'app/src/main/kotlin/io/github/nku100/impad/ImPadModule.kt',
        ]
        validate_paths(allowed, 'wechat')
        with self.assertRaisesRegex(ValueError, 'outside the allowed'):
            validate_paths(['app/src/main/kotlin/io/github/nku100/wechatpad/Legacy.kt'], 'wechat')

    def test_adaptation_allows_additive_rule_sources_but_not_historical_targets(self):
        validate_paths([
            'compat-rules/src/main/kotlin/io/github/nku100/impad/rules/qq/QqNextRule.kt',
            'compat-rules/src/test/kotlin/io/github/nku100/impad/rules/qq/QqNextRuleTest.kt',
            'compat-rules/src/main/kotlin/io/github/nku100/impad/rules/RuleCatalog.kt',
        ], 'qq')
        with self.assertRaisesRegex(ValueError, 'outside the allowed'):
            validate_paths(['compatibility/qq/targets.json'], 'qq')

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

    def test_wecom_policy_preserves_its_dedicated_rule_configuration(self):
        policy = get_policy("wecom")
        self.assertTrue(Path(policy.adaptation_prompt).is_file(), policy.adaptation_prompt)
        self.assertEqual(policy.required_hook_ids, ("device_type", "app_pad", "service_pad"))

    def test_test_only_second_policy_is_confined_to_its_own_paths(self):
        test_policy = replace(
            get_policy("wechat"), app_id="testapp", package_name="org.example.testapp",
            app_source_prefix="app/src/main/kotlin/example/testapp/",
            shared_source_paths=(), artifact_prefix="test-only",
        )
        with patch.dict(POLICIES, {"testapp": test_policy}):
            validate_paths(["app/src/main/kotlin/example/testapp/Adapter.kt"], "testapp")
            for path in ["compatibility/testapp/targets.json", "compatibility/wechat/targets.json",
                         "app/src/main/kotlin/io/github/nku100/impad/apps/wechat/Adapter.kt"]:
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "outside the allowed"):
                    validate_paths([path], "testapp")


if __name__ == "__main__":
    unittest.main()
