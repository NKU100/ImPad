import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts.ci.app_policy import get_policy
from scripts.ci.semantic_regression_baseline import load_verified_regression_pool
from scripts.ci.codex_adaptation import (
    _stage_regression_apks, _validate_rules, _worktree_tree, candidate_key, eligible_candidate,
    gradle_command, run_bounded, sha256, validate_existing_tests, validate_paths,
)


class CodexAdaptationTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.pool = load_verified_regression_pool(self.root, 'wechat')
        self.record = self.pool.cases[0]
        static = self.record['savedStaticReport']
        catalog = static['ruleCatalog']
        recognition = {'appId': 'wechat', 'evaluatedRecognizerIds': static['evaluatedRecognizerIds'],
                       'ruleCatalogSha256': catalog['ruleCatalogSha256']}
        recognition_sha = hashlib.sha256(json.dumps(
            recognition, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.report = {
            'schemaVersion': 2, 'appId': 'wechat', 'status': 'INCOMPATIBLE',
            'identity': dict(static['identity'], apkSha256='a' * 64),
            'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/fixed.apk',
            'ruleCatalog': catalog,
            'snapshot': static['snapshot'], 'ruleCatalogSha256': catalog['ruleCatalogSha256'],
            'recognition': recognition, 'recognitionSha256': recognition_sha,
            'diagnostic': {'selection': 'NO_MATCH'},
        }

    def test_candidate_eligibility_checks_the_complete_v2_corpus(self):
        self.assertTrue(eligible_candidate(self.report, [self.record], 'wechat'))
        same = copy.deepcopy(self.report)
        same['identity']['apkSha256'] = self.record['identity']['apkSha256']
        self.assertFalse(eligible_candidate(same, [self.record], 'wechat'))

    def test_candidate_identity_and_source_are_revalidated(self):
        for field, value in [('packageName', 'org.example.other'),
                             ('signerSha256', 'd' * 64), ('abi', 'x86_64')]:
            report = copy.deepcopy(self.report)
            report['identity'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                eligible_candidate(report, [self.record], 'wechat')
        report = copy.deepcopy(self.report)
        report['sourceUrl'] = 'http://example.invalid/candidate.apk'
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            eligible_candidate(report, [self.record], 'wechat')

    def test_rules_acceptance_loads_and_rechecks_verified_v2_pool(self):
        from scripts.ci import codex_adaptation
        pool = SimpleNamespace(cases=[self.record], assert_unchanged=MagicMock())
        catalog = {'schemaVersion': 2, 'appId': 'wechat', 'recognizerIds': ['wechat_shape']}
        with patch('scripts.ci.codex_adaptation.load_verified_regression_pool', return_value=pool) as load, \
             patch('scripts.ci.codex_adaptation._load_semantic_catalog', return_value=catalog) as current:
            result = codex_adaptation._rules_acceptance(Path('/repo'), 'wechat')
        self.assertEqual((catalog, pool), result)
        load.assert_called_once_with(Path('/repo'), 'wechat')
        self.assertEqual(2, pool.assert_unchanged.call_count)
        current.assert_called_once_with('wechat', Path('/repo'))

    def test_eligibility_cli_unpacks_the_v2_pool_and_checks_all_cases(self):
        from scripts.ci.codex_adaptation import main
        pool = SimpleNamespace(cases=[self.record], assert_unchanged=MagicMock())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / 'candidate.json'
            report_path.write_text(json.dumps(self.report))
            output = root / 'github-output'
            with patch('sys.argv', ['codex_adaptation', 'eligibility', '--app-id', 'wechat',
                                    '--repository', str(root), '--report', str(report_path)]), \
                 patch.dict('os.environ', {'GITHUB_OUTPUT': str(output)}), \
                 patch('scripts.ci.codex_adaptation._rules_acceptance', return_value=({}, pool)), \
                 patch('scripts.ci.codex_adaptation._validate_candidate_static'), \
                 patch('scripts.ci.codex_adaptation.eligible_candidate', return_value=True) as eligible:
                main()
            eligible.assert_called_once_with(self.report, pool.cases, 'wechat')
            self.assertIn('adaptation_eligible=true', output.read_text())

    def test_v2_table_and_every_archived_evidence_file_are_immutable(self):
        from scripts.ci.codex_adaptation import _validate_v2_tree
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '--allow-empty', '-qm', 'base'], cwd=root, check=True)
            archive = root / 'compatibility/wechat/archive'
            archive.mkdir(parents=True)
            (archive / 'manifest.json').write_text('{}')
            (archive / 'static.json').write_text('{"saved":true}')
            table_path = root / 'compatibility/wechat/acceptance-v2.json'
            table_path.write_text(json.dumps({'records': [{
                'evidenceArchive': {'manifestPath': 'compatibility/wechat/archive/manifest.json',
                                    'files': [{'path': 'compatibility/wechat/archive/static.json'}]}}]}))
            subprocess.run(['git', 'add', 'compatibility'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'v2 data'], cwd=root, check=True)
            base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
            pool = SimpleNamespace(app_id='wechat', v2_sha256=hashlib.sha256(
                table_path.read_bytes()).hexdigest(), assert_unchanged=MagicMock())
            proposed = _worktree_tree(root, base)
            _validate_v2_tree(root, base, proposed, pool)
            (archive / 'static.json').write_text('{"saved":false}')
            changed = _worktree_tree(root, base)
            with self.assertRaisesRegex(ValueError, 'evidence archive changed'):
                _validate_v2_tree(root, base, changed, pool)

    def test_prepare_stages_one_cached_candidate_per_selected_group_with_splits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / 'cache'
            apk_dir = root / 'work/apks'
            cache.mkdir()
            apk_dir.mkdir(parents=True)
            base, split = b'cached base', b'cached split'
            base_hash = hashlib.sha256(base).hexdigest()
            split_hash = hashlib.sha256(split).hexdigest()
            (cache / f'{base_hash}.apk').write_bytes(base)
            (cache / f'{split_hash}.apk').write_bytes(split)
            make_case = lambda record_id, code, digest: {
                'recordId': record_id, 'appId': 'wechat',
                'identity': {'packageName': 'com.tencent.mm', 'apkSha256': digest,
                             'versionCode': code, 'versionName': f'8.0.{code}',
                             'abi': 'arm64-v8a'},
                'source': {'url': 'https://dldir1v6.qq.com/weixin/android/fixed.apk'},
                'apkFiles': [{'role': 'base', 'path': 'base.apk', 'sha256': digest},
                             {'role': 'split', 'path': 'split.apk', 'sha256': split_hash}],
            }
            first = make_case('first', 10, hashlib.sha256(b'not cached').hexdigest())
            second = make_case('second', 9, base_hash)
            pool = SimpleNamespace(app_id='wechat')
            selected = {'selectedGroups': [{'groupId': 'behavior', 'cases': [first, second]}]}
            with patch('scripts.ci.codex_adaptation.regression_selection', return_value=selected), \
                 patch('scripts.ci.codex_adaptation.ensure_apk') as fetch:
                _selection, staged = _stage_regression_apks(pool, cache, apk_dir)
            fetch.assert_not_called()
            self.assertEqual([('8.0.9', 'work/apks/9-' + base_hash + '.apk', base_hash)], staged)
            self.assertFalse((apk_dir / f'10-{base_hash}.apk').exists())
            self.assertEqual([base, split], [(apk_dir / f'{base_hash}.apk').read_bytes(),
                                             (apk_dir / f'{split_hash}.apk').read_bytes()])

    def test_grouped_regression_is_replayed_and_failures_reject_validation(self):
        from scripts.ci.grouped_static_regression import regression_baseline_binding, regression_selection

        class ReachedBuild(Exception):
            pass

        for outcome in ('PASSED', 'FAILED'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                worktree = root / 'checkout'
                worktree.mkdir()
                subprocess.run(['git', 'init', '-q', str(worktree)], check=True)
                (worktree / '.gitignore').write_text('work/\n.cache/\n')
                subprocess.run(['git', 'add', '.gitignore'], cwd=worktree, check=True)
                subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                'commit', '-qm', 'base'], cwd=worktree, check=True)
                base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=worktree, text=True).strip()
                report = copy.deepcopy(self.report)
                candidate = worktree / 'work/apks'
                candidate.mkdir(parents=True)
                candidate_bytes = b'exact candidate APK'
                digest = hashlib.sha256(candidate_bytes).hexdigest()
                report['identity']['apkSha256'] = digest
                (candidate / f'candidate-{digest}.apk').write_bytes(candidate_bytes)
                report['apkFiles'] = [{'path': 'base.apk', 'role': 'base', 'sha256': digest}]
                analysis = worktree / 'work/analysis'
                analysis.mkdir()
                (analysis / 'base.apk').write_bytes(candidate_bytes)
                runtime = worktree / 'work/runtime-smoke'
                runtime.mkdir(parents=True)
                static = {'selection': {'status': 'SELECTED'}, 'executionIdentity': {'checker': 'current'}}
                static_path = runtime / 'static-check.json'
                static_path.write_text(json.dumps(static))
                (worktree / 'work/analysis/adaptation-summary.json').write_text('{}')

                pool = self.pool
                selection = regression_selection(pool)
                selected_ids = [group['groupId'] for group in selection['selectedGroups']]
                binding = regression_baseline_binding(pool)
                state = {'base': base, 'agent_base': base, 'existing_tests': {}, 'report': report,
                    'acceptanceSnapshotSha256': pool.v2_sha256, 'v2SnapshotSha256': pool.v2_sha256,
                    'regressionBaseline': binding, 'regressionRecordIds': selected_ids,
                    'apks': [('8.0.80', f'work/apks/candidate-{digest}.apk', digest)], 'static': static}
                from scripts.ci.candidate_selection import recognition_identity
                # The prepared candidate must carry a checker-valid semantic identity.
                from scripts.test_candidate_selection import CandidateSelectionTest
                semantic = CandidateSelectionTest._semantic_report('wechat', candidate / f'candidate-{digest}.apk')
                semantic['selection']['status'] = 'SELECTED'
                static_path.write_text(json.dumps(semantic))
                static = semantic
                state['static'] = static
                recognition, recognition_sha = recognition_identity(semantic)

                def recheck(*_args, **_kwargs):
                    return {'staticReport': 'static-check.json', 'expectedPlan': semantic['selection']['plan'],
                        'expectedExecutionSha256': semantic['selection']['executionSha256'],
                        'ruleCatalog': semantic['ruleCatalog'], 'snapshot': semantic['snapshot'],
                        'recognition': recognition, 'recognitionSha256': recognition_sha,
                        'ruleCatalogSha256': semantic['ruleCatalog']['ruleCatalogSha256'],
                        'apkFiles': report['apkFiles']}

                grouped = {'groups': [{'groupId': selected_ids[0], 'status': outcome}], 'status': outcome}
                with patch('scripts.ci.codex_adaptation._rules_acceptance', return_value=(semantic['ruleCatalog'], pool)), \
                     patch('scripts.ci.codex_adaptation._validate_v2_tree'), \
                     patch('scripts.ci.codex_adaptation._worktree_tree', return_value='tree'), \
                     patch('scripts.ci.candidate_selection.prepare_candidate_from_adaptation_report', side_effect=recheck), \
                     patch('scripts.ci.codex_adaptation.run_grouped_regression', return_value=grouped), \
                     patch('scripts.ci.codex_adaptation.verify_grouped_regression', return_value=grouped), \
                     patch('scripts.ci.codex_adaptation.badge_regression_rows',
                           return_value=[{'status': outcome}]), \
                     patch('scripts.ci.candidate_selection.write_local_runtime_assets'), \
                     patch('scripts.ci.codex_adaptation.run_bounded', side_effect=ReachedBuild):
                    if outcome == 'PASSED':
                        with self.assertRaises(ReachedBuild):
                            _validate_rules(root, state, get_policy('wechat'), worktree)
                    else:
                        with self.assertRaisesRegex(ValueError, 'REGRESSION_FAILED'):
                            _validate_rules(root, state, get_policy('wechat'), worktree)

    def test_paths_and_gradle_gate_remain_bounded(self):
        validate_paths([
            'app/src/main/kotlin/io/github/nku100/impad/apps/wechat/WeChatAppAdapter.kt',
            'app/src/test/kotlin/io/github/nku100/impad/apps/wechat/WeChatAppAdapterTest.kt',
            'compat-rules/src/main/kotlin/io/github/nku100/impad/rules/wechat/WeChatRule.kt',
        ], 'wechat')
        with self.assertRaisesRegex(ValueError, 'outside'):
            validate_paths(['compatibility/wechat/acceptance-v2.json'], 'wechat')
        self.assertEqual([
            './gradlew', '--offline', '--no-daemon', ':app:testDebugUnitTest',
            ':app:testReleaseUnitTest', ':compat-core:test', ':compat-rules:test',
            ':compat-checker:test', ':app:verifyModuleMetadata', ':app:assembleDebug',
        ], gradle_command('wechat'))

    def test_existing_tests_are_hash_protected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            test = root / 'ExistingTest.kt'
            test.write_text('assert rejects ambiguous rule\n')
            validate_existing_tests(root, {'ExistingTest.kt': sha256(test)})
            test.write_text('assert true\n')
            with self.assertRaisesRegex(ValueError, 'regression tests'):
                validate_existing_tests(root, {'ExistingTest.kt': '0' * 64})

    def test_model_waits_without_an_agent_owned_timeout(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch('scripts.ci.codex_adaptation.subprocess.Popen') as launch:
            process = MagicMock()
            process.wait.return_value = 0
            launch.return_value = process
            run_bounded(['codex'], Path(temporary), {}, Path(temporary) / 'model.log')
            process.wait.assert_called_once_with(timeout=None)

    def test_proposed_tree_uses_private_index_and_preserves_real_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            def run_git(*args):
                return subprocess.check_output(['git', *args], cwd=repository).decode().strip()
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
            original_index = (repository / '.git/index').read_bytes()
            source.write_text('candidate\n')
            (repository / 'new.txt').write_text('new candidate\n')
            tree = _worktree_tree(repository, base)
            self.assertEqual('candidate', run_git('show', f'{tree}:source.txt'))
            self.assertEqual('staged', run_git('show', ':source.txt'))
            self.assertEqual(original_index, (repository / '.git/index').read_bytes())

    def test_explicit_timeout_terminates_process_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(TimeoutError):
                run_bounded(['python3', '-c', 'import time; time.sleep(30)'],
                            Path(temporary), {}, Path(temporary) / 'timeout.log', timeout=0.1)


if __name__ == '__main__':
    unittest.main()
