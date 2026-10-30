import unittest

from scripts.ci.apps.wechat import runtime_policy
from scripts.test_semantic_hook_evidence import lifecycle_fixture as semantic_lifecycle_fixture


PACKAGE = 'com.tencent.mm'
MODULE = 'io.github.nku100.impad'
ACTIVITY = lambda name: (
    f'ACTIVITY MANAGER ACTIVITIES\n  topResumedActivity=ActivityRecord{{abc u0 {PACKAGE}/.{name} t8}}'
)


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
            'app': {'packageName': PACKAGE, 'baseApkPath': '/data/app/mm/base.apk', 'baseApkSha256': 'a' * 64,
                    'splitApks': [{'path': '/data/app/mm/split_config.arm64_v8a.apk', 'sha256': 'c' * 64}]},
            'module': {'packageName': MODULE, 'baseApkPath': '/data/app/impad/base.apk', 'baseApkSha256': 'b' * 64},
        }
        runtime_policy.derive_installed_identities(identities, PACKAGE, 'a' * 64, MODULE, 'b' * 64,
                                                   apk_files=[{'role': 'base', 'sha256': 'a' * 64},
                                                              {'role': 'split', 'sha256': 'c' * 64}])
        with self.assertRaises(ValueError):
            runtime_policy.derive_installed_identities(identities, PACKAGE, 'a' * 64, MODULE, 'b' * 64,
                                                       apk_files=[{'role': 'base', 'sha256': 'a' * 64},
                                                                  {'role': 'split', 'sha256': 'd' * 64}])
        with self.assertRaises(ValueError):
            runtime_policy.derive_installed_identities(identities, PACKAGE, 'a' * 64, MODULE, 'b' * 64,
                                                       apk_files=[{'role': 'base', 'sha256': 'a' * 64}])
        identities['module']['baseApkSha256'] = 'c' * 64
        with self.assertRaises(ValueError):
            runtime_policy.derive_installed_identities(identities, PACKAGE, 'a' * 64, MODULE, 'b' * 64)

    def test_lifecycle_replay_binds_complete_plan_native_identity_and_event_order(self):
        candidate, module, process, logs, installed, _ = semantic_lifecycle_fixture('wechat')

        diagnostics, lifecycle = runtime_policy.derive_hook_evidence(
            process, logs, candidate, module, installed)

        self.assertEqual('COMPATIBLE', diagnostics['compatibility'])
        self.assertEqual('INSTALLED_2', diagnostics['hookInstallation'])
        self.assertEqual('READY', lifecycle['tinkerEvidence']['status'])
        self.assertEqual('LOADED', lifecycle['nativeEvidence']['status'])
        self.assertEqual(candidate['expectedPlan'], lifecycle['hookEvidence']['completePlan'])
        self.assertEqual(candidate['expectedExecutionSha256'], lifecycle['hookEvidence']['executionSha256'])
        self.assertTrue(lifecycle['timingEvidence']['nativeBeforeHook'])
        with self.assertRaises(ValueError):
            runtime_policy.derive_hook_evidence(process, logs + logs, candidate, module)

    def test_semantic_selection_carries_plan_without_rule_id_projection(self):
        candidate, module, process, logs, _, _ = semantic_lifecycle_fixture('wechat')
        _, lifecycle = runtime_policy.derive_hook_evidence(process, logs, candidate, module)
        self.assertNotIn('ruleId', lifecycle['hookEvidence'])
        self.assertEqual(candidate['expectedPlan']['contractId'], lifecycle['hookEvidence']['contractId'])
        self.assertEqual(candidate['expectedPlan']['strategyId'], lifecycle['hookEvidence']['strategyId'])

    def test_semantic_plan_payload_and_hook_contract_mismatches_fail(self):
        candidate, module, process, logs, _, _ = semantic_lifecycle_fixture('wechat')
        replacements = (
            ('executionSha256=' + candidate['expectedExecutionSha256'], 'executionSha256=' + 'f' * 64),
            ('contractId=wechat_contract', 'contractId=other_contract'),
            ('strategyId=wechat_strategy', 'strategyId=other_strategy'),
        )
        for original, replacement in replacements:
            with self.subTest(original=original), self.assertRaises(ValueError):
                runtime_policy.derive_hook_evidence(process, logs.replace(original, replacement), candidate, module)

    def test_candidate_requires_schema_two_complete_plan_and_execution_hash(self):
        for mutation in (
            lambda candidate: candidate.update(schemaVersion=1),
            lambda candidate: candidate.update(expectedExecutionSha256='not-a-hash'),
            lambda candidate: candidate.update(expectedPlan={}),
        ):
            candidate, module, process, logs, _, _ = semantic_lifecycle_fixture('wechat')
            mutation(candidate)
            with self.assertRaises(ValueError):
                runtime_policy.derive_hook_evidence(process, logs, candidate, module)


if __name__ == '__main__':
    unittest.main()
