import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.ci import runtime_task
from scripts.ci.apps.wechat import runtime_policy


PACKAGE = 'com.tencent.mm'
MODULE = 'io.github.nku100.impad'
APK_SHA = 'a' * 64
MODULE_SHA = 'b' * 64
API_SHA = 'c' * 64
NATIVE_SHA = 'd' * 64


class WeChatRawRuntimeReplayTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app_root = self.root / 'apps/wechat'
        self.app_root.mkdir(parents=True)
        self.candidate = {
            'appId': 'wechat',
            'identity': {'packageName': PACKAGE, 'versionCode': 3040, 'abi': 'arm64-v8a',
                         'apkSha256': APK_SHA},
            'apkFiles': [{'role': 'base', 'path': 'input/wechat.apk', 'sha256': APK_SHA}],
            'expectedPlan': {'ruleId': 'wechat_tablet_login_001',
                             'strategyId': 'wechat_tablet_login_behavior_001'},
        }
        self.module = {'sha256': MODULE_SHA, 'dexkit': {
            'dexkitAndroidApiSha256': API_SHA, 'dexkitAndroidArm64NativeSha256': NATIVE_SHA,
        }}
        self.smoke = self._write_valid_capture()

    def tearDown(self):
        self.temporary.cleanup()

    def test_verifier_replays_raw_ui_identity_manager_and_lifecycle_evidence(self):
        files = runtime_task._validate_wechat_raw_evidence(self.smoke, self.root, self.candidate, self.module)
        self.assertEqual(16, len(files))
        self.assertEqual('INSTALLED_2', self.smoke['runtimeDiagnostics']['hookInstallation'])
        self.assertEqual('READY', self.smoke['runtimeLifecycleEvidence']['tinkerEvidence']['status'])

    def test_summary_claim_cannot_replace_tablet_entry_in_captured_ui(self):
        self.smoke['baselineEvidence']['tabletEntry'] = True
        with self.assertRaises(ValueError):
            runtime_task._validate_wechat_raw_evidence(self.smoke, self.root, self.candidate, self.module)

    def test_refreshed_hash_does_not_make_wrong_ui_hierarchy_valid(self):
        path = self.app_root / 'module-on-login.xml'
        path.write_text((self.app_root / 'module-off-login.xml').read_text())
        self._refresh(path)
        with self.assertRaises(ValueError):
            runtime_task._validate_wechat_raw_evidence(self.smoke, self.root, self.candidate, self.module)

    def test_installed_module_identity_is_replayed_against_the_tested_module(self):
        path = self.app_root / 'installed-identities.json'
        identities = json.loads(path.read_text())
        identities['module']['baseApkSha256'] = 'e' * 64
        path.write_text(json.dumps(identities) + '\n')
        self._refresh(path)
        with self.assertRaises(ValueError):
            runtime_task._validate_wechat_raw_evidence(self.smoke, self.root, self.candidate, self.module)

    def test_missing_capture_and_duplicate_reference_fail_closed(self):
        self.smoke['evidence'] = [row for row in self.smoke['evidence']
                                  if row['path'] != 'apps/wechat/manager-state-disabled.json']
        with self.assertRaisesRegex(ValueError, 'missing required'):
            runtime_task._validate_wechat_raw_evidence(self.smoke, self.root, self.candidate, self.module)
        self.smoke = self._write_valid_capture()
        self.smoke['evidence'].append(self.smoke['evidence'][0])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            runtime_task._validate_wechat_raw_evidence(self.smoke, self.root, self.candidate, self.module)

    def _write_valid_capture(self):
        activities = {
            'module-off-login-activity.txt': self._activity('MobileInputUI'),
            'module-on-login-activity.txt': self._activity('MobileInputUI'),
            'tablet-qr-current-activity.txt': self._activity('LoginAsExDeviceUI'),
            'tablet-qr-confirmed-activity.txt': self._activity('LoginAsExDeviceUI'),
        }
        for name, value in activities.items():
            (self.app_root / name).write_text(value)
        (self.app_root / 'module-off-login.xml').write_text(
            f'<hierarchy><node package="{PACKAGE}" text="登录" /></hierarchy>')
        (self.app_root / 'module-on-login.xml').write_text(
            f'<hierarchy><node package="{PACKAGE}" text="登录时手机和平板同时登录" /></hierarchy>')
        for name in ('tablet-qr-current.xml', 'tablet-qr-confirmed.xml'):
            (self.app_root / name).write_text(
                f'<hierarchy><node package="{PACKAGE}" text="Scan QR code with your phone" /></hierarchy>')

        states = {}
        for stage, enabled in (('initial', 1), ('disabled', 0), ('enabled', 1), ('final', 1)):
            raw = {'schemaVersion': 1, 'module': MODULE, 'packageName': PACKAGE,
                   'moduleStateRows': [{'modulePackage': MODULE, 'userId': 0, 'enabled': enabled}],
                   'scopeRows': [{'modulePackage': MODULE, 'appPackage': PACKAGE, 'userId': 0}]}
            path = self.app_root / f'manager-state-{stage}.json'
            path.write_text(json.dumps(raw) + '\n')
            states[stage] = runtime_policy.derive_manager_state(raw, MODULE, PACKAGE)

        installed = {
            'schemaVersion': 1,
            'app': {'packageName': PACKAGE, 'baseApkPath': '/data/app/mm/base.apk',
                    'baseApkSha256': APK_SHA},
            'module': {'packageName': MODULE, 'baseApkPath': '/data/app/impad/base.apk',
                       'baseApkSha256': MODULE_SHA},
        }
        (self.app_root / 'installed-identities.json').write_text(json.dumps(installed) + '\n')

        process_text = f'3100 {PACKAGE}\n'
        native_path = '/data/app/impad/base.apk'
        timestamp = lambda second: f'10-21 00:00:{second:02}.000'
        envelope = f'[{MODULE},ImPad,1.0.0,1,1] '
        messages = [
            (1, f'status=COMPATIBLE rule plan=wechat_tablet_login_001/wechat_tablet_login_behavior_001 versionCode=3040 apkInputs=1 cacheHit=false'),
            (2, 'waiting for Tinker class loader'),
            (3, 'rule-runtime event=tinker_ready elapsedRealtimeNs=100 contextPackage=com.tencent.mm loaderClass=example.ReadyLoader'),
            (4, f'rule-runtime event=native_loaded elapsedRealtimeNs=200 apiSha256={API_SHA} nativeSha256={NATIVE_SHA} nativeAbi=arm64-v8a packagingMode=APK_DIRECT sourceApkPath={native_path} nativeLoadPath={native_path}!/lib/arm64-v8a/libdexkit.so'),
            (5, 'installed 2 WeChat hooks'),
            (6, 'rule-runtime event=hook_installed elapsedRealtimeNs=300 ruleId=wechat_tablet_login_001 strategyId=wechat_tablet_login_behavior_001 hookCount=2 processName=com.tencent.mm'),
        ]
        logcat = ''.join(f'{timestamp(second)} 3100 3100 I ImPad: ({PACKAGE}){envelope}{body}\n'
                         for second, body in messages)
        (self.app_root / 'processes.txt').write_text(process_text)
        (self.app_root / 'logcat.txt').write_text(logcat)
        scoped = runtime_policy.scoped_module_log_lines(logcat, process_text, PACKAGE)
        (self.app_root / 'app-hook-logs.txt').write_text(scoped)

        samples = {
            'baseline': {'activity': activities['module-off-login-activity.txt'],
                         'hierarchy': (self.app_root / 'module-off-login.xml').read_text()},
            'enabled': {'activity': activities['module-on-login-activity.txt'],
                        'hierarchy': (self.app_root / 'module-on-login.xml').read_text()},
            'qrCurrent': {'activity': activities['tablet-qr-current-activity.txt'],
                          'hierarchy': (self.app_root / 'tablet-qr-current.xml').read_text()},
            'qrConfirmed': {'activity': activities['tablet-qr-confirmed-activity.txt'],
                            'hierarchy': (self.app_root / 'tablet-qr-confirmed.xml').read_text()},
        }
        ui = runtime_policy.derive_ui_evidence(samples, PACKAGE, 'LoginAsExDeviceUI')
        diagnostics, lifecycle = runtime_policy.derive_hook_evidence(
            process_text, scoped, self.candidate, self.module, installed['module'])
        smoke = {
            'status': 'RUNTIME_SMOKE_VERIFIED',
            'baselineEvidence': dict(states['disabled'], **ui['baseline']),
            'enabled': dict(states['enabled'], **ui['enabled']),
            'managerStates': states, 'initialManagerState': states['initial'],
            'finalManagerState': states['final'], 'installedIdentities': installed,
            'moduleApkSha256': MODULE_SHA, 'qrPage': ui['qrPage'], 'qrStable': ui['qrStable'],
            'runtimeDiagnostics': diagnostics, 'runtimeLifecycleEvidence': lifecycle,
            'evidence': [],
        }
        smoke['evidence'] = [
            {'path': path.relative_to(self.root).as_posix(),
             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(self.app_root.iterdir()) if path.is_file()
        ]
        return smoke

    def _refresh(self, path):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        relative = path.relative_to(self.root).as_posix()
        for row in self.smoke['evidence']:
            if row['path'] == relative:
                row['sha256'] = digest
                return
        raise AssertionError(f'Missing raw evidence reference: {relative}')

    @staticmethod
    def _activity(name):
        return (f'ACTIVITY MANAGER ACTIVITIES\n  topResumedActivity=ActivityRecord{{abc u0 '
                f'{PACKAGE}/.{name} t8}}')


if __name__ == '__main__':
    unittest.main()
