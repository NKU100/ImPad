import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts.ci.acceptance import RoutingReviewRequired
from scripts.ci.codex_adaptation import (
    candidate_key, eligible_candidate, gradle_command, run_bounded,
    prepare_rules, sha256, validate_existing_tests, validate_paths,
    _worktree_tree,
)


class CodexAdaptationTest(unittest.TestCase):
    def test_proposed_tree_uses_private_index_and_preserves_real_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            def run_git(*arguments):
                return subprocess.check_output(['git', *arguments], cwd=repository).decode().strip()
            run_git('init', '-q')
            run_git('config', 'user.name', 'Test')
            run_git('config', 'user.email', 'test@example.invalid')
            source = repository / 'source.txt'
            source.write_text('base\n')
            run_git('add', 'source.txt')
            run_git('commit', '-qm', 'base')
            base = run_git('rev-parse', 'HEAD')
            source.write_text('staged\n')
            run_git('add', 'source.txt')
            index_before = (repository / '.git/index').read_bytes()
            source.write_text('candidate\n')
            (repository / 'new.txt').write_text('new candidate\n')
            proposed = _worktree_tree(repository, base)
            self.assertEqual('candidate', run_git('show', f'{proposed}:source.txt'))
            self.assertEqual('new candidate', run_git('show', f'{proposed}:new.txt'))
            self.assertEqual('staged', run_git('show', ':source.txt'))
            self.assertEqual(index_before, (repository / '.git/index').read_bytes())
            self.assertEqual(base, run_git('rev-parse', 'HEAD'))

    def setUp(self):
        self.record = {
            'recordId': 'wechat-first', 'appId': 'wechat',
            'identity': {'packageName': 'com.tencent.mm', 'versionName': '8.0.69',
                         'versionCode': 3040, 'abi': 'arm64-v8a',
                         'apkSha256': 'b' * 64,
                         'signerSha256': '0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c'},
        }
        self.report = {
            'schemaVersion': 1, 'appId': 'wechat', 'status': 'INCOMPATIBLE',
            'identity': {'packageName': 'com.tencent.mm', 'versionName': '8.0.80',
                         'versionCode': 3220, 'abi': 'arm64-v8a',
                         'apkSha256': 'a' * 64,
                         'signerSha256': '0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c'},
            'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/weixin8080android3220_arm64.apk',
        }

    def test_forward_candidate_is_keyed_by_exact_app_identity(self):
        self.assertEqual('wechat-com.tencent.mm-arm64-v8a-3220-' + 'a' * 64,
                         candidate_key(self.report))
        self.assertTrue(eligible_candidate(self.report, [self.record]))

    def test_known_digest_is_not_readapted_and_unknown_old_build_needs_routing_review(self):
        same = copy.deepcopy(self.report)
        same['identity']['apkSha256'] = self.record['identity']['apkSha256']
        self.assertFalse(eligible_candidate(same, [self.record]))

        older = copy.deepcopy(self.report)
        older['identity']['versionCode'] = 3040
        with self.assertRaises(RoutingReviewRequired):
            eligible_candidate(older, [self.record])

    def test_identity_and_official_source_are_revalidated(self):
        for field, value in [('packageName', 'org.example.other'),
                             ('signerSha256', 'c' * 64), ('abi', 'x86_64')]:
            report = copy.deepcopy(self.report)
            report['identity'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                eligible_candidate(report, [self.record])
        report = copy.deepcopy(self.report)
        report['sourceUrl'] = 'https://example.invalid/candidate.apk'
        with self.assertRaisesRegex(ValueError, 'official Tencent CDN'):
            eligible_candidate(report, [self.record])

    def test_only_app_rule_runtime_and_focused_test_paths_are_allowed(self):
        validate_paths([
            'app/src/main/kotlin/io/github/nku100/impad/apps/wechat/WeChatAppAdapter.kt',
            'app/src/test/kotlin/io/github/nku100/impad/apps/wechat/WeChatAppAdapterTest.kt',
            'compat-rules/src/main/kotlin/io/github/nku100/impad/rules/wechat/WeChatRule.kt',
            'compat-rules/src/test/kotlin/io/github/nku100/impad/rules/wechat/WeChatRuleTest.kt',
        ], 'wechat')
        for path in ('compatibility/wechat/acceptance.json', 'compatibility/wechat/targets.json',
                     '.github/workflows/build.yml',
                     'app/src/main/kotlin/io/github/nku100/impad/apps/qq/QqAppAdapter.kt'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'outside'):
                validate_paths([path], 'wechat')

    def test_gradle_validation_build_has_no_candidate_asset_mode(self):
        command = gradle_command('wechat')
        self.assertIn(':app:testDebugUnitTest', command)
        self.assertIn(':compat-rules:test', command)
        self.assertIn(':app:verifyModuleMetadata', command)
        self.assertIn(':app:assembleDebug', command)
        self.assertFalse(any('runtimeLocalAssets' in argument for argument in command))

    def test_existing_tests_are_hash_protected(self):
        with tempfile.TemporaryDirectory() as temporary:
            test = Path(temporary) / 'ExistingTest.kt'
            test.write_text('assert rejects ambiguous rule\n')
            baseline = {'ExistingTest.kt': sha256(test)}
            validate_existing_tests(Path(temporary), baseline)
            test.write_text('assert true\n')
            with self.assertRaisesRegex(ValueError, 'regression tests'):
                validate_existing_tests(Path(temporary), baseline)
            test.unlink()
            with self.assertRaisesRegex(ValueError, 'regression tests'):
                validate_existing_tests(Path(temporary), baseline)

    def test_model_command_waits_without_an_agent_owned_timeout(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch('scripts.ci.codex_adaptation.subprocess.Popen') as launch:
            process = MagicMock()
            process.wait.return_value = 0
            launch.return_value = process
            run_bounded(['codex'], Path(temporary), {}, Path(temporary) / 'model.log')
            process.wait.assert_called_once_with(timeout=None)

    def test_prepare_rules_passes_full_acceptance_record_to_real_cached_ensure_apk(self):
        class ReachedCheckout(Exception):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / 'repo'
            repository.mkdir()
            cache = repository / '.cache/wechat-apks'
            cache.mkdir(parents=True)
            accepted_bytes = b'fixed-accepted-apk-fixture'
            accepted_digest = hashlib.sha256(accepted_bytes).hexdigest()
            accepted_apk = cache / f'{accepted_digest}.apk'
            accepted_apk.write_bytes(accepted_bytes)
            accepted = copy.deepcopy(self.record)
            accepted['identity']['apkSha256'] = accepted_digest
            accepted['source'] = {
                'type': 'official',
                'url': 'https://dldir1v6.qq.com/weixin/android/weixin8069android3040_0x2800455a_arm64.apk',
            }
            report = copy.deepcopy(self.report)
            candidate_bytes = b'incompatible-candidate-fixture'
            candidate_apk = root / 'candidate.apk'
            candidate_apk.write_bytes(candidate_bytes)
            report['identity']['apkSha256'] = hashlib.sha256(candidate_bytes).hexdigest()
            report_path = root / 'candidate-report.json'
            report_path.write_text(json.dumps(report))
            snapshot = SimpleNamespace(records=[accepted], sha256='f' * 64)
            (root / 'adaptation/checkout').mkdir(parents=True)

            copied_sources = []

            def stop_after_cache_hit(source, destination):
                copied_sources.append(Path(source))
                if len(copied_sources) == 2:
                    self.assertEqual(accepted_apk, Path(source))
                    raise ReachedCheckout('accepted cache passed')

            with patch('scripts.ci.codex_adaptation._rules_acceptance', return_value=(snapshot, {})), \
                 patch('scripts.ci.codex_adaptation.eligible_candidate', return_value=True), \
                 patch('scripts.ci.acceptance.select_representatives', return_value=[accepted]), \
                 patch('scripts.ci.codex_adaptation.git', side_effect=['base', 'agent-base']), \
                 patch('scripts.ci.codex_adaptation.subprocess.run'), \
                 patch('scripts.ci.codex_adaptation.shutil.copyfile', side_effect=stop_after_cache_hit):
                with self.assertRaisesRegex(ReachedCheckout, 'accepted cache passed'):
                    prepare_rules(repository, report_path, candidate_apk, root / 'adaptation')

    def test_explicit_command_timeout_terminates_the_process_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(TimeoutError):
                run_bounded(['python3', '-c', 'import time; time.sleep(30)'],
                            Path(temporary), {}, Path(temporary) / 'timeout.log', timeout=0.1)


if __name__ == '__main__':
    unittest.main()
