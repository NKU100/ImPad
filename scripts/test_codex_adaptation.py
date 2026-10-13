import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

from scripts.ci.codex_adaptation import candidate_key, eligible_candidate, validate_profiles, validate_paths, run_bounded, validate_existing_tests, sha256, gradle_command


class CodexAdaptationTest(unittest.TestCase):
    def test_wecom_validation_serializes_gradle_workers_without_changing_wechat_command(self):
        self.assertEqual(['./gradlew', '--offline', '--no-daemon', ':app:testDebugUnitTest',
                          ':compat-core:test', ':compat-checker:test', ':app:assembleDebug'],
                         gradle_command('wechat'))
        self.assertEqual(['./gradlew', '--offline', '--no-daemon', '--no-parallel', '--max-workers=2',
                          ':app:testDebugUnitTest', ':compat-core:test', ':compat-checker:test', ':app:assembleDebug'],
                         gradle_command('wecom'))

    def setUp(self):
        self.targets = json.loads(Path('compatibility/wechat/targets.json').read_text())
        self.identity = dict(self.targets[-1]['identity'], versionName='8.0.80', versionCode=3220, apkSha256='a' * 64)
        self.report = {'appId': 'wechat', 'identity': self.identity, 'status': 'NEEDS_HOOK_REVIEW',
                       'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/weixin8080android3220_arm64.apk'}
        self.profile = copy.deepcopy(self.targets[-1])
        self.profile.update(identity=self.identity, sourceUrl=self.report['sourceUrl'], verificationStatus='static-verified')

    def test_preparation_preserves_repository_context_in_an_independent_checkout(self):
        from scripts.ci.codex_adaptation import prepare, git
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / 'repository'
            repository.mkdir()
            subprocess.run(['git', 'init', '-q', str(repository)], check=True)
            for path in ['.gitignore', 'gradlew', 'gradlew.bat', 'gradle.properties',
                         'build.gradle.kts', 'settings.gradle.kts', 'gradle/fixture',
                         'app/src/test/fixture', 'compat-core/fixture', 'compat-checker/fixture']:
                file = repository / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('synthetic')
            targets = repository / 'compatibility/wechat/targets.json'
            targets.parent.mkdir(parents=True)
            targets.write_text(json.dumps(self.targets))
            (repository / 'old-answer.md').write_text('historical adaptation')
            subprocess.run(['git', 'add', '.'], cwd=repository, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                            'commit', '-qm', 'Old answer'], cwd=repository, check=True)
            (repository / 'old-answer.md').write_text('current adaptation')
            subprocess.run(['git', 'add', 'old-answer.md'], cwd=repository, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                            'commit', '-qm', 'Update context'], cwd=repository, check=True)
            (repository / 'controller-state.json').write_text('untracked controller state')
            apk = root / 'candidate.apk'
            apk.write_bytes(b'candidate')
            report = dict(self.report, identity=dict(self.identity, apkSha256=sha256(apk)),
                          hooks=['secret answer'], suggestedProfile={'answer': True})
            report_path = root / 'report.json'
            report_path.write_text(json.dumps(report))
            task = root / 'task'
            with patch('scripts.ci.codex_adaptation.select_regression_targets', return_value=[]):
                prepare(repository, report_path, apk, task)
            checkout = task / 'checkout'
            self.assertEqual('current adaptation', (checkout / 'old-answer.md').read_text())
            self.assertEqual('historical adaptation', git(checkout, 'show', 'HEAD~1:old-answer.md'))
            self.assertEqual('2', git(checkout, 'rev-list', '--count', 'HEAD'))
            self.assertFalse((checkout / 'controller-state.json').exists())
            self.assertFalse((checkout / '.git/objects/info/alternates').exists())
            (checkout / 'old-answer.md').write_text('agent changes')
            self.assertEqual('current adaptation', (repository / 'old-answer.md').read_text())
            self.assertEqual('', git(checkout, 'remote'))
            supplied = json.loads((checkout / 'work/analysis/candidate-report.json').read_text())
            self.assertEqual(report, supplied)
            state = json.loads((task / 'state.json').read_text())
            self.assertEqual(git(repository, 'rev-parse', 'HEAD'), state['base'])
            self.assertEqual(git(checkout, 'rev-parse', 'HEAD'), state['agent_base'])
            self.assertEqual(state['base'], state['agent_base'])

    def test_preparation_uses_wecom_policy_for_regression_apk(self):
        from scripts.ci.codex_adaptation import prepare
        baseline_bytes = b'cached baseline'
        baseline = {
            'identity': {'packageName': 'com.tencent.wework', 'versionName': '5.0.8',
                         'versionCode': 70675, 'abi': 'arm64-v8a',
                         'apkSha256': hashlib.sha256(baseline_bytes).hexdigest(),
                         'signerSha256': '66896ae0aae48b9e963a3e034fa2ca813ca2f661f53619a92263a8a5c2e3f388'},
            'sourceUrl': 'https://dldir1.qq.com/wework/work_weixin/wecom-baseline-test.apk',
            'verificationStatus': 'runtime-verified-local',
            'hooks': [{'id': hook} for hook in ('device_type', 'app_pad', 'service_pad')],
        }
        targets = [baseline]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / 'repository'
            repository.mkdir()
            subprocess.run(['git', 'init', '-q', str(repository)], check=True)
            targets_path = repository / 'compatibility/wecom/targets.json'
            targets_path.parent.mkdir(parents=True)
            targets_path.write_text(json.dumps(targets))
            subprocess.run(['git', 'add', '.'], cwd=repository, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                            'commit', '-qm', 'Add WeCom baseline'], cwd=repository, check=True)

            candidate_apk = root / 'candidate.apk'
            candidate_apk.write_bytes(b'wecom candidate')
            identity = dict(baseline['identity'], versionName='5.0.12', versionCode=83835,
                            apkSha256=sha256(candidate_apk))
            report = {'appId': 'wecom', 'identity': identity, 'status': 'NEEDS_HOOK_REVIEW',
                      'sourceUrl': 'https://dldir1.qq.com/wework/work_weixin/WeCom_android_5.0.12.83835_arm64_100001.apk'}
            report_path = root / 'report.json'
            report_path.write_text(json.dumps(report))
            cache = repository / '.cache/wecom-apks'
            cache.mkdir(parents=True)
            (cache / f"{baseline['identity']['apkSha256']}.apk").write_bytes(baseline_bytes)

            with patch('scripts.ci.static_regression.download_candidate',
                       side_effect=AssertionError('Regression fixture must not download APKs')):
                prepare(repository, report_path, candidate_apk, root / 'task')

            state = json.loads((root / 'task/state.json').read_text())
            self.assertEqual(['5.0.12', '5.0.8'], [entry[0] for entry in state['apks']])

    def test_validation_uses_wecom_policy_for_shared_checker(self):
        from scripts.ci.codex_adaptation import validate
        targets = json.loads(Path('compatibility/wecom/targets.json').read_text())
        baseline = targets[0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / 'repository'
            repository.mkdir()
            subprocess.run(['git', 'init', '-q', str(repository)], check=True)
            (repository / '.gitignore').write_text('work/\n')
            targets_path = repository / 'compatibility/wecom/targets.json'
            targets_path.parent.mkdir(parents=True)
            targets_path.write_text(json.dumps(targets))
            subprocess.run(['git', 'add', '.'], cwd=repository, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                            'commit', '-qm', 'Add WeCom baseline'], cwd=repository, check=True)
            base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository, text=True).strip()

            apk = repository / 'work/apks/candidate.apk'
            apk.parent.mkdir(parents=True)
            apk.write_bytes(b'wecom candidate')
            identity = dict(baseline['identity'], versionName='5.0.12', versionCode=83835,
                            apkSha256=sha256(apk))
            source_url = 'https://dldir1.qq.com/wework/work_weixin/WeCom_android_5.0.12.83835_arm64_100001.apk'
            report = {'appId': 'wecom', 'identity': identity, 'status': 'NEEDS_HOOK_REVIEW',
                      'sourceUrl': source_url}
            profile = copy.deepcopy(baseline)
            profile.update(identity=identity, sourceUrl=source_url, verificationStatus='static-verified')
            targets_path.write_text(json.dumps(targets + [profile]))

            task = root / 'task'
            task.mkdir()
            (task / 'state.json').write_text(json.dumps({
                'appId': 'wecom', 'targetsPath': 'compatibility/wecom/targets.json',
                'worktree': str(repository), 'base': base, 'agent_base': base,
                'before': targets, 'report': report,
                'apks': [['5.0.12', 'work/apks/candidate.apk', identity['apkSha256']]],
                'existing_tests': {},
            }))

            def check_with_policy(repository, targets_path, apk, digest, log,
                                  offline=False, app_id='wechat'):
                if app_id != 'wecom':
                    raise ValueError('Shared checker was not given the WeCom application policy')

            with patch('scripts.ci.codex_adaptation.run_bounded'), \
                    patch('scripts.ci.codex_adaptation.check_apk', side_effect=check_with_policy) as checker_run:
                validate(task)

            checker_run.assert_called_once()

    def test_unknown_verified_candidate_can_run_even_when_static_inference_passed(self):
        self.assertTrue(eligible_candidate(self.report, self.targets))
        self.report['status'] = 'STATIC_VERIFIED_PENDING_RUNTIME'
        self.assertTrue(eligible_candidate(self.report, self.targets))
        self.assertEqual('wechat-com.tencent.mm-arm64-v8a-3220-' + 'a' * 64, candidate_key(self.report))

    def test_older_unknown_build_cannot_bypass_the_latest_only_gate(self):
        baseline_code = max(p['identity']['versionCode'] for p in self.targets)
        self.report['identity'].update(versionName='0.0.1', versionCode=baseline_code - 1)
        self.report['manualSelection'] = True
        with self.assertRaisesRegex(ValueError, 'older'):
            eligible_candidate(self.report, self.targets)

    def test_hosted_verified_profile_is_a_trusted_baseline(self):
        self.targets[-1]['verificationStatus'] = 'runtime-verified-hosted'
        self.assertTrue(eligible_candidate(self.report, self.targets))

    def test_known_version_or_rejected_identity_does_not_consume_model_usage(self):
        self.report['identity'] = self.targets[-1]['identity']
        self.assertFalse(eligible_candidate(self.report, self.targets))

    def test_same_version_new_digest_is_eligible_but_exact_registered_digest_is_not(self):
        same_version = copy.deepcopy(self.targets[-1])
        same_version["identity"].update(versionName="8.0.79", versionCode=3200)
        same_version["identity"]["apkSha256"] = "b" * 64
        same_version["verificationStatus"] = "runtime-verified-hosted"
        targets = self.targets + [same_version]
        self.report["identity"].update(versionName="8.0.79", versionCode=3200, apkSha256="f" * 64)
        self.assertTrue(eligible_candidate(self.report, targets))
        self.report["identity"]["apkSha256"] = same_version["identity"]["apkSha256"]
        self.assertFalse(eligible_candidate(self.report, targets))
        self.report['identity'] = self.identity
        self.report['status'] = 'IDENTITY_REJECTED'
        self.assertFalse(eligible_candidate(self.report, self.targets))

    def test_eligibility_rechecks_package_signer_and_source(self):
        for field, value in [('packageName', 'other.app'), ('signerSha256', 'b' * 64), ('abi', 'x86_64')]:
            report = copy.deepcopy(self.report)
            report['identity'][field] = value
            with self.assertRaises(ValueError):
                eligible_candidate(report, self.targets)
        self.report['sourceUrl'] = 'https://example.com/app.apk'
        with self.assertRaises(ValueError):
            eligible_candidate(self.report, self.targets)

    def test_candidate_profile_is_static_only_and_preserves_every_old_profile(self):
        validate_profiles(self.targets, self.targets + [self.profile], self.report)
        changed = copy.deepcopy(self.targets + [self.profile])
        changed[0]['hooks'][0]['expectedDescriptor'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'existing'):
            validate_profiles(self.targets, changed, self.report)
        self.profile['verificationStatus'] = 'runtime-verified-local'
        with self.assertRaisesRegex(ValueError, 'static-verified'):
            validate_profiles(self.targets, self.targets + [self.profile], self.report)

    def test_wecom_profile_keeps_exact_descriptors_without_inventing_string_anchors(self):
        before = json.loads(Path('compatibility/wecom/targets.json').read_text())
        baseline = before[0]
        identity = dict(baseline['identity'], versionName='5.0.12', versionCode=83835,
                        apkSha256='b' * 64)
        source = 'https://dldir1.qq.com/wework/work_weixin/WeCom_android_5.0.12.83835_arm64_100001.apk'
        report = {'appId': 'wecom', 'identity': identity, 'status': 'NEEDS_HOOK_REVIEW', 'sourceUrl': source}
        profile = copy.deepcopy(baseline)
        profile.update(identity=identity, sourceUrl=source, verificationStatus='static-verified')

        validate_profiles(before, before + [profile], report, 'wecom')

        missing_descriptor = copy.deepcopy(profile)
        missing_descriptor['hooks'][0]['expectedDescriptor'] = ''
        with self.assertRaisesRegex(ValueError, 'descriptor or locator'):
            validate_profiles(before, before + [missing_descriptor], report, 'wecom')
        unsafe_anchor = copy.deepcopy(profile)
        unsafe_anchor['hooks'][0]['stringAnchor'] = 'invented anchor'
        with self.assertRaisesRegex(ValueError, 'descriptor or locator'):
            validate_profiles(before, before + [unsafe_anchor], report, 'wecom')

    def test_wecom_agent_can_add_focused_tests_without_touching_other_apps(self):
        validate_paths([
            'app/src/test/kotlin/io/github/nku100/impad/apps/wecom/WeComTargetProfileTest.kt',
            'compat-core/src/test/kotlin/io/github/nku100/impad/compat/WeComProfileTest.kt',
        ], 'wecom')
        with self.assertRaisesRegex(ValueError, 'allowed'):
            validate_paths(['app/src/test/kotlin/io/github/nku100/impad/apps/qq/OtherAppTest.kt'], 'wecom')

    def test_profile_validation_retains_every_variant_and_rejects_duplicate_full_key(self):
        old_variant = copy.deepcopy(self.targets[-1])
        old_variant["identity"]["apkSha256"] = "b" * 64
        before = self.targets + [old_variant]
        validate_profiles(before, before + [self.profile], self.report)
        duplicate = copy.deepcopy(self.profile)
        with self.assertRaisesRegex(ValueError, "(?i)duplicate.*profile"):
            validate_profiles(before, before + [self.profile, duplicate], self.report)

    def test_agent_cannot_register_wrong_identity_or_change_validators(self):
        self.profile['identity'] = dict(self.identity, apkSha256='c' * 64)
        with self.assertRaisesRegex(ValueError, 'identity'):
            validate_profiles(self.targets, self.targets + [self.profile], self.report)
        validate_paths(['compatibility/wechat/targets.json', 'compat-core/src/main/kotlin/io/github/nku100/impad/compat/CandidatePipeline.kt'])
        for path in ['compatibility/targets.json', 'compatibility/qq/targets.json', '.github/workflows/build.yml', 'scripts/ci/codex_adaptation.py', 'gradle.properties', 'compat-checker/src/main/kotlin/Main.kt', 'compat-core/src/main/kotlin/io/github/nku100/impad/compat/Unexpected.kt']:
            with self.assertRaisesRegex(ValueError, 'allowed'):
                validate_paths([path])

    def test_explicit_unknown_app_and_cross_app_profile_fail_closed(self):
        report = copy.deepcopy(self.report)
        report['appId'] = 'qq'
        with self.assertRaisesRegex(ValueError, 'Unknown or unregistered appId'):
            eligible_candidate(report, self.targets)
        report = copy.deepcopy(self.report)
        report['identity']['packageName'] = 'org.example.other'
        with self.assertRaisesRegex(ValueError, 'package'):
            eligible_candidate(report, self.targets)

    def test_model_has_no_custom_execution_timeout(self):
        with tempfile.TemporaryDirectory() as temp, patch('scripts.ci.codex_adaptation.subprocess.Popen') as launch:
            process = MagicMock()
            process.wait.return_value = 0
            launch.return_value = process
            run_bounded(['codex'], Path(temp), {}, Path(temp) / 'output')
            process.wait.assert_called_once_with(timeout=None)

    def test_explicit_timeout_stops_a_bounded_command(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(TimeoutError):
                run_bounded(['python3', '-c', 'import time; time.sleep(30)'], Path(temp), {}, Path(temp) / 'output', timeout=0.1)

    def test_agent_cannot_weaken_or_delete_existing_regression_tests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            test = root / 'Test.kt'
            test.write_text('assert rejects ambiguous Hook')
            original = {'Test.kt': sha256(test)}
            validate_existing_tests(root, original)
            test.write_text('assert true')
            with self.assertRaisesRegex(ValueError, 'regression tests'):
                validate_existing_tests(root, original)
            test.unlink()
            with self.assertRaisesRegex(ValueError, 'regression tests'):
                validate_existing_tests(root, original)


if __name__ == '__main__':
    unittest.main()
