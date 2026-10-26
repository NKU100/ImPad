import unittest

from scripts.ci.apps.wecom.runtime_policy import (
    derive_installed_identities,
    hook_diagnostics,
    launcher_component,
    login_input_ready,
    qr_page_ready,
    tablet_entry_visible,
)


PACKAGE = "com.tencent.wework"
LOGIN_ACTIVITY = "ACTIVITY MANAGER ACTIVITIES\n  topResumedActivity=ActivityRecord{abc u0 com.tencent.wework/.login.controller.LoginWxAuthActivity t8}"
QR_ACTIVITY = "ACTIVITY MANAGER ACTIVITIES\n  topResumedActivity=ActivityRecord{abc u0 com.tencent.wework/.login.controller.LoginQrCodeForAndroidPad t9}"


class WeComRuntimePolicyTest(unittest.TestCase):
    def test_installed_split_hashes_must_match_all_candidate_inputs(self):
        raw = {'schemaVersion': 1,
               'app': {'packageName': PACKAGE, 'baseApkPath': '/data/app/wecom/base.apk',
                       'baseApkSha256': 'a' * 64,
                       'splitApks': [{'path': '/data/app/wecom/split_config.apk', 'sha256': 'b' * 64}]},
               'module': {'packageName': 'io.github.nku100.impad',
                          'baseApkPath': '/data/app/impad/base.apk', 'baseApkSha256': 'c' * 64}}
        candidate_files = [{'role': 'base', 'sha256': 'a' * 64}, {'role': 'split', 'sha256': 'b' * 64}]
        derive_installed_identities(raw, PACKAGE, 'a' * 64, 'io.github.nku100.impad', 'c' * 64,
                                    apk_files=candidate_files)
        for bad_files in (candidate_files[:1], candidate_files + [{'role': 'split', 'sha256': 'd' * 64}]):
            with self.subTest(bad_files=bad_files), self.assertRaises(ValueError):
                derive_installed_identities(raw, PACKAGE, 'a' * 64, 'io.github.nku100.impad', 'c' * 64,
                                            apk_files=bad_files)

    def test_clean_install_uses_exported_launch_activity(self):
        self.assertEqual(launcher_component, "com.tencent.wework/.launch.LaunchSplashActivity")

    def test_tablet_entry_matches_expected_english_and_chinese_labels(self):
        self.assertTrue(tablet_entry_visible("Login on pad and mobile phone at the same time"))
        self.assertTrue(tablet_entry_visible("平板和手机同时登录"))
        self.assertFalse(tablet_entry_visible("Continue with Phone"))

    def test_first_launch_privacy_dialog_is_not_mistaken_for_login_ready(self):
        privacy = f'<hierarchy><node package="{PACKAGE}" text="Privacy Policy" /></hierarchy>'
        login = f'<hierarchy><node package="{PACKAGE}" text="Continue with Phone" /></hierarchy>'
        self.assertFalse(login_input_ready(LOGIN_ACTIVITY, privacy, PACKAGE))
        self.assertTrue(login_input_ready(LOGIN_ACTIVITY, login, PACKAGE))

    def test_tablet_qr_gate_requires_expected_activity_and_app_ui(self):
        ui = f'<hierarchy><node package="{PACKAGE}" text="Scan the QR code with WeCom on your phone to log in" /></hierarchy>'
        self.assertTrue(qr_page_ready(QR_ACTIVITY, ui, PACKAGE, "LoginQrCodeForAndroidPad"))
        self.assertFalse(qr_page_ready(LOGIN_ACTIVITY, ui, PACKAGE, "LoginQrCodeForAndroidPad"))
        self.assertFalse(qr_page_ready(QR_ACTIVITY, ui.replace(PACKAGE, "other.app"), PACKAGE,
                                       "LoginQrCodeForAndroidPad"))

    def test_activity_gate_rejects_inner_text_and_hidden_qr_content(self):
        ui = f'<hierarchy><node package="{PACKAGE}" text="QR code" visible="false" /></hierarchy>'
        nested = f'log says topResumedActivity=ActivityRecord{{abc u0 {PACKAGE}/.LoginQrCodeForAndroidPad t9}}'
        self.assertFalse(qr_page_ready(nested, ui, PACKAGE, 'LoginQrCodeForAndroidPad'))
        self.assertFalse(qr_page_ready(QR_ACTIVITY, ui, PACKAGE, 'LoginQrCodeForAndroidPad'))

    def test_hook_diagnostics_require_compatibility_and_installation_records(self):
        good = "I ImPad: status=COMPATIBLE\nI ImPad: installed 3 WeCom hooks"
        evidence = hook_diagnostics(good)
        self.assertEqual(evidence["compatibility"], "COMPATIBLE")
        self.assertEqual(evidence["hookInstallation"], "INSTALLED_3")
        self.assertEqual(hook_diagnostics("I ImPad: module loaded")["hookInstallation"], "NOT_OBSERVED")


if __name__ == "__main__":
    unittest.main()
