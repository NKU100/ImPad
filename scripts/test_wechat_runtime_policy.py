import unittest

from scripts.ci.apps.wechat import runtime_policy


PACKAGE = 'com.tencent.mm'
MODULE = 'io.github.nku100.impad'
ACTIVITY = lambda name: (
    f'ACTIVITY MANAGER ACTIVITIES\n  topResumedActivity=ActivityRecord{{abc u0 {PACKAGE}/.{name} t8}}'
)


def lifecycle_fixture(rule_id="wechat_tablet_login_001", strategy_id="wechat_tablet_login_behavior_001"):
    candidate = {
        'identity': {'packageName': PACKAGE, 'versionCode': 3040, 'abi': 'arm64-v8a'},
        'apkFiles': [{'path': 'input/wechat.apk'}],
        'expectedPlan': {'ruleId': rule_id,
                         'strategyId': strategy_id},
    }
    module = {'dexkit': {'dexkitAndroidApiSha256': 'c' * 64,
                         'dexkitAndroidArm64NativeSha256': 'd' * 64}}
    process = f'3100 {PACKAGE}\n'
    prefix = f'10-21 00:00:01.000 3100 3100 I ImPad: ({PACKAGE})[{MODULE},ImPad,1.0.0,1,1] '
    logs = '\n'.join((
        prefix + f'status=COMPATIBLE rule plan={rule_id}/{strategy_id} versionCode=3040 apkInputs=1 cacheHit=false',
        prefix + 'waiting for Tinker class loader',
        prefix + 'rule-runtime event=tinker_ready elapsedRealtimeNs=100 contextPackage=com.tencent.mm loaderClass=example.ReadyLoader',
        prefix + 'rule-runtime event=native_loaded elapsedRealtimeNs=200 apiSha256=' + 'c' * 64 +
        ' nativeSha256=' + 'd' * 64 + ' nativeAbi=arm64-v8a packagingMode=APK_DIRECT sourceApkPath=/data/app/impad/base.apk nativeLoadPath=/data/app/impad/base.apk!/lib/arm64-v8a/libdexkit.so',
        prefix + 'installed 2 WeChat hooks',
        prefix + f'rule-runtime event=hook_installed elapsedRealtimeNs=300 ruleId={rule_id} strategyId={strategy_id} hookCount=2 processName=com.tencent.mm',
    )) + '\n'
    return candidate, module, process, logs


class WeChatStrictRuntimePolicyTest(unittest.TestCase):
    def test_ui_evidence_replays_module_toggle_and_stable_qr_from_raw_captures(self):
        login = f'<hierarchy><node package="{PACKAGE}" text="登录" /></hierarchy>'
        enabled = (f'<hierarchy><node package="{PACKAGE}" text="登录" />'
                   f'<node package="{PACKAGE}" text="登录时手机和平板同时登录" /></hierarchy>')
        qr = f'<hierarchy><node package="{PACKAGE}" text="Scan QR code with your phone" /></hierarchy>'
        samples = {
            'baseline': {'activity': ACTIVITY('MobileInputUI'), 'hierarchy': login},
            'enabled': {'activity': ACTIVITY('MobileInputUI'), 'hierarchy': enabled},
            'qrCurrent': {'activity': ACTIVITY('LoginAsExDeviceUI'), 'hierarchy': qr},
            'qrConfirmed': {'activity': ACTIVITY('LoginAsExDeviceUI'), 'hierarchy': qr},
        }

        evidence = runtime_policy.derive_ui_evidence(samples, PACKAGE, 'LoginAsExDeviceUI')

        self.assertFalse(evidence['baseline']['tabletEntry'])
        self.assertTrue(evidence['enabled']['tabletEntry'])
        self.assertTrue(evidence['qrStable'])
        samples['enabled']['hierarchy'] = login
        with self.assertRaises(ValueError):
            runtime_policy.derive_ui_evidence(samples, PACKAGE, 'LoginAsExDeviceUI')

    def test_manager_and_installed_identity_evidence_are_schema_and_hash_bound(self):
        manager = {
            'schemaVersion': 1, 'module': MODULE, 'packageName': PACKAGE,
            'moduleStateRows': [{'modulePackage': MODULE, 'userId': 0, 'enabled': 1}],
            'scopeRows': [{'modulePackage': MODULE, 'appPackage': PACKAGE, 'userId': 0}],
        }
        self.assertEqual({'moduleEnabled': True, 'scopeVerified': True},
                         runtime_policy.derive_manager_state(manager, MODULE, PACKAGE))
        manager['scopeRows'].append(manager['scopeRows'][0])
        with self.assertRaises(ValueError):
            runtime_policy.derive_manager_state(manager, MODULE, PACKAGE)

        identities = {
            'schemaVersion': 1,
            'app': {'packageName': PACKAGE, 'baseApkPath': '/data/app/mm/base.apk', 'baseApkSha256': 'a' * 64},
            'module': {'packageName': MODULE, 'baseApkPath': '/data/app/impad/base.apk', 'baseApkSha256': 'b' * 64},
        }
        runtime_policy.derive_installed_identities(identities, PACKAGE, 'a' * 64, MODULE, 'b' * 64)
        identities['module']['baseApkSha256'] = 'c' * 64
        with self.assertRaises(ValueError):
            runtime_policy.derive_installed_identities(identities, PACKAGE, 'a' * 64, MODULE, 'b' * 64)

    def test_lifecycle_replay_binds_target_plan_native_identity_and_event_order(self):
        candidate, module, process, logs = lifecycle_fixture()

        diagnostics, lifecycle = runtime_policy.derive_hook_evidence(process, logs, candidate, module, {
            'baseApkPath': '/data/app/impad/base.apk',
        })

        self.assertEqual('COMPATIBLE', diagnostics['compatibility'])
        self.assertEqual('INSTALLED_2', diagnostics['hookInstallation'])
        self.assertEqual('READY', lifecycle['tinkerEvidence']['status'])
        self.assertEqual('LOADED', lifecycle['nativeEvidence']['status'])
        self.assertTrue(lifecycle['timingEvidence']['nativeBeforeHook'])
        with self.assertRaises(ValueError):
            runtime_policy.derive_hook_evidence(process, logs + logs, candidate, module)


    def test_selected_plan_ids_are_replayed_without_a_legacy_rule_gate(self):
        candidate, module, process, logs = lifecycle_fixture('synthetic_rule', 'synthetic_strategy')
        _, lifecycle = runtime_policy.derive_hook_evidence(process, logs, candidate, module)
        self.assertEqual('synthetic_rule', lifecycle['hookEvidence']['ruleId'])
        self.assertEqual('synthetic_strategy', lifecycle['hookEvidence']['strategyId'])

    def test_selected_plan_still_rejects_status_and_hook_identity_mismatches(self):
        candidate, module, process, logs = lifecycle_fixture('synthetic_rule', 'synthetic_strategy')
        replacements = (
            ('plan=synthetic_rule/', 'plan=other_rule/'),
            ('/synthetic_strategy versionCode=', '/other_strategy versionCode='),
            ('ruleId=synthetic_rule ', 'ruleId=other_rule '),
            ('strategyId=synthetic_strategy ', 'strategyId=other_strategy '),
        )
        for original, replacement in replacements:
            with self.subTest(original=original), self.assertRaises(ValueError):
                runtime_policy.derive_hook_evidence(process, logs.replace(original, replacement), candidate, module)

    def test_plan_ids_must_be_unambiguous_log_field_values(self):
        for field in ('ruleId', 'strategyId'):
            for value in (None, 17, True, '', 'two words', 'two/parts', 'line\nbreak', 'tab\tvalue', ' leading'):
                candidate, module, process, logs = lifecycle_fixture()
                candidate['expectedPlan'][field] = value
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'expectation is incomplete'):
                    runtime_policy.derive_hook_evidence(process, logs, candidate, module)


if __name__ == '__main__':
    unittest.main()
