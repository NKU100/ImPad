from pathlib import Path
import unittest

from scripts.ci.app_policy import get_policy
from scripts.ci.apps.qq.discovery_policy import discover_from_config
from scripts.ci.apps.qq.runtime_policy import validate_request_evidence
from scripts.test_qq_requests import proof, expectation


class QqPolicyTest(unittest.TestCase):
    def test_login_activity_without_visible_inputs_is_not_ready(self):
        from scripts.ci.apps.qq.runtime_policy import login_input_ready
        package = 'com.tencent.mobileqq'
        activity = 'com.tencent.mobileqq/.activity.LoginActivity'
        welcome = '<hierarchy><node package="com.tencent.mobileqq" class="android.widget.Button" text="登录"/></hierarchy>'
        self.assertFalse(login_input_ready(activity, welcome, package))
        for visible, expected in [('false', False), ('true', True)]:
            xml = '<hierarchy><node package="com.tencent.mobileqq" class="android.widget.EditText" visible="' + visible + '" bounds="[0,10][100,50]"/></hierarchy>'
            self.assertEqual(expected, login_input_ready(activity, xml, package))

    def test_official_config_selects_arm64_download(self):
        config = {'android': {'version': '9.3.70', 'x64Link':
            'https://downv6.qq.com/qqweb/QQ_1/android_apk/9.3.70.41925_2a98ecf56e5ef04b.apk',
            'x32Link': 'https://example.org/wrong.apk'}}
        result = discover_from_config(config, get_policy('qq').official_apk_prefix)
        self.assertEqual(result.version_code, 41925)
        with self.assertRaises(ValueError):
            discover_from_config({'android': dict(config['android'], x64Link='https://example.org/qq.apk')},
                                 get_policy('qq').official_apk_prefix)
        with self.assertRaises(ValueError):
            discover_from_config({'android': dict(config['android'], version='9.3.75')},
                                 get_policy('qq').official_apk_prefix)

    def test_manifest_expected_values_are_parsed_from_both_configurations(self):
        from scripts.ci.apps.qq.runtime_probe import manifest_app_ids
        xml = '<manifest xmlns:android="http://schemas.android.com/apk/res/android"><application>'
        xml += '<meta-data android:name="AppSetting_params" android:value="10#token" />'
        xml += '<meta-data android:name="AppSetting_params_pad" android:value="20#token" />'
        xml += '</application></manifest>'
        self.assertEqual(manifest_app_ids(xml), (10, 20))
        with self.assertRaises(ValueError):
            manifest_app_ids(xml.replace('20#token', '10#token'))
        with self.assertRaises(ValueError):
            manifest_app_ids(xml.replace('</application>', '<meta-data android:name="AppSetting_params" android:value="10#token" /></application>'))

    def test_runtime_promotion_requires_request_evidence(self):
        from scripts.ci.runtime_task import runtime_result
        identity = {'packageName': 'com.tencent.mobileqq', 'versionName': '9.3.1',
                    'versionCode': 14378, 'abi': 'arm64-v8a',
                    'apkSha256': 'a' * 64,
                    'signerSha256': get_policy('qq').signer_sha256}
        report = dict(appId='qq', status='STATIC_VERIFIED_PENDING_RUNTIME', identity=identity,
                      requestIdentityExpectation=expectation(identity['apkSha256']))
        smoke = dict(status='RUNTIME_SMOKE_VERIFIED', appId='qq', packageName='com.tencent.mobileqq',
                     abi='arm64-v8a', baseline='PHONE_REQUEST_APP_ID', hookIds=['device_type'],
                     apkSha256=identity['apkSha256'], moduleApkSha256='b'*64)
        self.assertEqual(runtime_result(report, smoke, 'success')['status'], 'RUNTIME_REJECTED')
        smoke.update(acceptanceKind='REQUEST_APP_ID_CONTRAST', requestIdentityEvidence=proof(identity['apkSha256']))
        self.assertEqual(runtime_result(report, smoke, 'success')['status'], 'RUNTIME_VERIFIED')
        smoke['requestIdentityEvidence']['enabled']['frames'][0]['packetAppId'] = 10
        self.assertEqual(runtime_result(report, smoke, 'success')['status'], 'RUNTIME_REJECTED')
        smoke['status'] = 'ACCEPTANCE_UNAVAILABLE'
        result = runtime_result(report, smoke, 'failure')
        self.assertEqual(result['status'], 'STATIC_VERIFIED_PENDING_RUNTIME')
        self.assertEqual(result['runtimeConclusion'], 'acceptance-unavailable')
        smoke.update(status='RUNTIME_SMOKE_VERIFIED', acceptanceKind='APP_ID_CONFIGURATION')
        self.assertEqual(runtime_result(report, smoke, 'success')['status'], 'RUNTIME_REJECTED')


if __name__ == '__main__':
    unittest.main()
