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
from scripts.ci.codex_adaptation import (
    candidate_key, eligible_candidate, gradle_command, run_bounded,
    prepare_rules, sha256, validate_existing_tests, validate_paths,
    _worktree_tree, _validate_rules,
)


class CodexAdaptationTest(unittest.TestCase):
    def test_rules_acceptance_uses_frozen_bundle_and_semantic_cli_catalog(self):
        from scripts.ci import codex_adaptation
        bundle = SimpleNamespace(profiles=[{'appId': 'wechat'}], historical_records=[self.record],
            history_sha256='1' * 64, expectations_sha256='2' * 64)
        baseline = SimpleNamespace(migration_bundle=bundle, cases=[{'recordId': 'fixed', 'kind': 'MIGRATION'}],
                                   sha256='3' * 64, v2_sha256=None,
                                   assert_unchanged=MagicMock())
        catalog = {'schemaVersion': 2, 'appId': 'wechat', 'recognizerIds': ['wechat_shape']}
        with patch('scripts.ci.semantic_regression_baseline.load_semantic_regression_baseline',
                   return_value=baseline) as load, \
             patch('scripts.ci.codex_adaptation._load_semantic_catalog', return_value=catalog) as current:
            result = codex_adaptation._rules_acceptance(Path('/repo'), 'wechat')
        self.assertEqual((bundle, catalog, baseline), result)
        load.assert_called_once_with(Path('/repo'), 'wechat')
        self.assertEqual(2, baseline.assert_unchanged.call_count)
        current.assert_called_once_with('wechat', Path('/repo'))

    def test_semantic_catalog_loader_calls_catalog_cli_and_rejects_legacy_shape(self):
        import shlex
        from scripts.ci.codex_adaptation import _load_semantic_catalog
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def run(command, **kwargs):
                arguments = shlex.split(command[-1].removeprefix('--args='))
                self.assertEqual(['catalog', '--app-id', 'wechat'], arguments[:3])
                Path(arguments[arguments.index('--report') + 1]).write_text(json.dumps(self.profile['ruleCatalog']))

            with patch('scripts.ci.codex_adaptation.subprocess.run', side_effect=run) as invoke:
                self.assertEqual(self.profile['ruleCatalog'], _load_semantic_catalog('wechat', root))
            self.assertEqual(':compat-checker:run', invoke.call_args.args[0][2])

            def legacy(command, **kwargs):
                arguments = shlex.split(command[-1].removeprefix('--args='))
                Path(arguments[arguments.index('--report') + 1]).write_text(json.dumps(
                    {'schemaVersion': 1, 'appId': 'wechat', 'definitions': [], 'bindings': [], 'strategyIds': []}))

            with patch('scripts.ci.codex_adaptation.subprocess.run', side_effect=legacy):
                with self.assertRaises(ValueError):
                    _load_semantic_catalog('wechat', root)

    def test_semantic_candidate_eligibility_does_not_apply_version_or_numbered_rule_routing(self):
        from scripts.ci.codex_adaptation import eligible_candidate
        old_build = copy.deepcopy(self.report)
        old_build['identity']['versionCode'] = 3040
        self.assertTrue(eligible_candidate(old_build, [self.record], 'wechat'))

    def test_candidate_validation_rejects_changed_frozen_oracle_hashes(self):
        from scripts.ci.codex_adaptation import _validate_frozen_oracle
        changed = SimpleNamespace(history_sha256='0' * 64, expectations_sha256=self.bundle.expectations_sha256,
            profiles=self.bundle.profiles, historical_records=self.bundle.historical_records)
        baseline = SimpleNamespace(sha256='3' * 64, v2_sha256=None,
                                   app_id='wechat',
                                   v2_path=Path('/candidate/compatibility/wechat/acceptance-v2.json'),
                                   cases=[{'kind': 'MIGRATION', 'recordId': self.profile['historicalRecordId'],
                                           'profileId': self.profile['profileId'],
                                           'acceptanceSnapshotSha256': self.profile['historicalAcceptanceSha256']}],
                                   assert_unchanged=MagicMock())
        state = {'migrationHistorySha256': self.bundle.history_sha256,
                 'migrationExpectationsSha256': self.bundle.expectations_sha256,
                 'acceptanceSnapshotSha256': self.profile['historicalAcceptanceSha256'],
                 'baselineSha256': '3' * 64, 'v2SnapshotSha256': None,
                 'regressionRecordIds': [self.profile['historicalRecordId']]}
        with patch('scripts.ci.codex_adaptation._rules_acceptance', return_value=(changed, {}, baseline)):
            with self.assertRaisesRegex(ValueError, 'oracle changed'):
                _validate_frozen_oracle(Path('/candidate'), state, get_policy('wechat'))

    def test_validation_runs_fixed_regression_in_the_candidate_repository(self):
        class ReachedBuild(Exception):
            pass

        import shutil
        from scripts.test_semantic_migration import SemanticMigrationTest, ROOT
        from scripts.ci.semantic_protocol import execution_identity_sha256
        for historical_matches in (True, False):
            with self.subTest(historical_matches=historical_matches), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                worktree = root / 'checkout'
                worktree.mkdir()
                subprocess.run(['git', 'init', '-q', str(worktree)], check=True)
                (worktree / '.gitignore').write_text('work/\n.cache/\n')
                shutil.copytree(ROOT / 'compatibility', worktree / 'compatibility')
                subprocess.run(['git', 'add', '.gitignore', 'compatibility'], cwd=worktree, check=True)
                subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                'commit', '-qm', 'ignore generated inputs'], cwd=worktree, check=True)
                base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=worktree, text=True).strip()
                report = copy.deepcopy(self.report)
                candidate = worktree / 'work/apks'
                candidate.mkdir(parents=True)
                candidate_bytes = b'exact candidate APK'
                candidate_digest = hashlib.sha256(candidate_bytes).hexdigest()
                report['identity']['apkSha256'] = candidate_digest
                (candidate / f'candidate-{candidate_digest}.apk').write_bytes(candidate_bytes)
                analysis = worktree / 'work/analysis'
                analysis.mkdir()
                (analysis / 'base.apk').write_bytes(candidate_bytes)
                report['apkFiles'] = [{'path': 'base.apk', 'role': 'base', 'sha256': candidate_digest}]
                baseline_case = {'kind': 'MIGRATION', 'recordId': self.profile['historicalRecordId'],
                    'profileId': self.profile['profileId'],
                    'sourceSha256': self.profile['historicalAcceptanceSha256']}
                regression_baseline = {'sha256': '3' * 64, 'appId': 'wechat',
                    'migrationHistorySha256': self.bundle.history_sha256,
                    'migrationExpectationsSha256': self.bundle.expectations_sha256,
                    'v2Path': 'compatibility/wechat/acceptance-v2.json',
                    'v2SnapshotSha256': None,
                    'regressionRecordIds': [baseline_case['recordId']],
                    'sourceCases': [baseline_case]}
                state = {'base': base, 'agent_base': base, 'existing_tests': {}, 'report': report,
                    'migrationHistorySha256': self.bundle.history_sha256,
                    'migrationExpectationsSha256': self.bundle.expectations_sha256,
                    'acceptanceSnapshotSha256': self.profile['historicalAcceptanceSha256'],
                    'baselineSha256': '3' * 64, 'v2SnapshotSha256': None,
                    'regressionRecordIds': [self.profile['historicalRecordId']],
                    'regressionBaseline': regression_baseline}
                from scripts.test_candidate_selection import CandidateSelectionTest
                staged_candidate = worktree / f'work/runtime-smoke/wechat-{candidate_digest}.apk'
                staged_candidate.parent.mkdir(parents=True, exist_ok=True)
                staged_candidate.write_bytes(candidate_bytes)
                semantic = CandidateSelectionTest._semantic_report(
                    'wechat', staged_candidate)
                semantic['identity'].update(report['identity'])
                semantic['inputHashes'][0].update(path='base.apk', sha256=candidate_digest)
                semantic['snapshot']['archives'][0]['containerSha256'] = candidate_digest
                execution = semantic['executionIdentity']
                execution['cacheIdentitySha256'] = execution_identity_sha256(execution)
                selected_path = worktree / 'work/runtime-smoke/static-check.json'
                selected_path.parent.mkdir(parents=True, exist_ok=True)
                selected_path.write_text(json.dumps(semantic))
                from scripts.ci.candidate_selection import recognition_identity
                recognition, recognition_sha = recognition_identity(semantic)

                def recheck(_app, _adaptation_report, runtime_root, repository=None):
                    return {'staticReport': 'static-check.json', 'expectedPlan': semantic['selection']['plan'],
                        'expectedExecutionSha256': semantic['selection']['executionSha256'],
                        'ruleCatalog': semantic['ruleCatalog'], 'snapshot': semantic['snapshot'],
                        'recognition': recognition, 'recognitionSha256': recognition_sha,
                        'ruleCatalogSha256': semantic['ruleCatalog']['ruleCatalogSha256'],
                        'apkFiles': report['apkFiles']}

                def regression(app_id, reports, cache=None, repository=None):
                    self.assertEqual('wechat', app_id)
                    self.assertEqual(worktree, repository)
                    rows = [{'status': 'PASSED' if historical_matches else 'FAILED',
                        'recordId': baseline_case['recordId'],
                        'baselineSha256': '3' * 64, 'v2SnapshotSha256': None,
                        'baselineSourceCase': baseline_case}]
                    reports.mkdir(parents=True, exist_ok=True)
                    (reports / 'results.json').write_text(json.dumps(rows))
                    return rows

                baseline = SimpleNamespace(sha256='3' * 64, v2_sha256=None,
                    app_id='wechat',
                    v2_path=worktree / 'compatibility/wechat/acceptance-v2.json',
                    cases=[{'recordId': self.profile['historicalRecordId'], 'kind': 'MIGRATION',
                            'profileId': self.profile['profileId'],
                            'acceptanceSnapshotSha256': self.profile['historicalAcceptanceSha256']}],
                    assert_unchanged=MagicMock())
                with patch('scripts.ci.codex_adaptation._rules_acceptance',
                           return_value=(self.bundle, self.profile['ruleCatalog'], baseline)), \
                     patch('scripts.ci.candidate_selection.prepare_candidate_from_adaptation_report', side_effect=recheck), \
                     patch('scripts.ci.codex_adaptation.run_regression', side_effect=regression), \
                     patch('scripts.ci.candidate_selection.write_local_runtime_assets'), \
                     patch('scripts.ci.codex_adaptation.run_bounded', side_effect=ReachedBuild):
                    if historical_matches:
                        with self.assertRaises(ReachedBuild):
                            _validate_rules(root, state, get_policy('wechat'), worktree)
                    else:
                        with self.assertRaisesRegex(ValueError, 'REGRESSION_FAILED'):
                            _validate_rules(root, state, get_policy('wechat'), worktree)
                rows = json.loads((worktree / 'work/runtime-smoke/regression/results.json').read_text())
                self.assertEqual('PASSED' if historical_matches else 'FAILED', rows[0]['status'])

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

    def test_proposed_tree_cannot_add_or_modify_the_pinned_v2_table(self):
        import shutil
        from scripts.ci.codex_adaptation import _validate_frozen_oracle_tree
        from scripts.test_semantic_migration import ROOT
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            subprocess.run(['git', 'init', '-q', str(repository)], check=True)
            shutil.copytree(ROOT / 'compatibility', repository / 'compatibility')
            subprocess.run(['git', 'add', 'compatibility'], cwd=repository, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'base without v2'], cwd=repository, check=True)
            base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository,
                                           text=True).strip()
            v2 = repository / 'compatibility/wechat/acceptance-v2.json'
            v2.write_bytes(b'{"schemaVersion":2}\n')
            proposed = _worktree_tree(repository, base)
            with self.assertRaisesRegex(ValueError, 'presence changed'):
                _validate_frozen_oracle_tree(repository, base, proposed, 'wechat', None)

            subprocess.run(['git', 'add', 'compatibility/wechat/acceptance-v2.json'],
                           cwd=repository, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'trusted v2 baseline'], cwd=repository, check=True)
            base_with_v2 = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository,
                                                   text=True).strip()
            original = v2.read_bytes()
            expected_sha = hashlib.sha256(original).hexdigest()
            v2.write_bytes(original + b' ')
            modified = _worktree_tree(repository, base_with_v2)
            with self.assertRaisesRegex(ValueError, 'table bytes changed'):
                _validate_frozen_oracle_tree(repository, base_with_v2, modified, 'wechat', expected_sha)

    def setUp(self):
        from scripts.ci.semantic_migration import load_migration_bundle
        from scripts.ci.candidate_selection import recognition_identity
        from scripts.test_semantic_migration import ROOT
        self.bundle = load_migration_bundle(ROOT)
        self.profile = next(row for row in self.bundle.profiles if row['appId'] == 'wechat')
        self.record = next(row for row in self.bundle.historical_records if row['appId'] == 'wechat')
        recognition = {'appId': 'wechat', 'evaluatedRecognizerIds': self.profile['ruleCatalog']['recognizerIds'],
                       'ruleCatalogSha256': self.profile['ruleCatalog']['ruleCatalogSha256']}
        recognition_sha = hashlib.sha256(json.dumps(recognition, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.report = {
            'schemaVersion': 2, 'appId': 'wechat', 'status': 'INCOMPATIBLE',
            'identity': {'packageName': 'com.tencent.mm', 'versionName': '8.0.80',
                         'versionCode': 3220, 'abi': 'arm64-v8a',
                         'apkSha256': 'a' * 64,
                         'signerSha256': '0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c'},
            'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/weixin8080android3220_arm64.apk',
            'ruleCatalog': copy.deepcopy(self.profile['ruleCatalog']),
            'snapshot': copy.deepcopy(self.profile['snapshot']),
            'ruleCatalogSha256': self.profile['ruleCatalog']['ruleCatalogSha256'],
            'recognition': recognition, 'recognitionSha256': recognition_sha,
            'diagnostic': {'selection': 'NO_MATCH', 'diagnostics': {}, 'incompleteReasons': {}},
        }

    def test_forward_candidate_is_keyed_by_exact_app_identity(self):
        self.assertEqual('wechat-com.tencent.mm-arm64-v8a-3220-' + 'a' * 64,
                         candidate_key(self.report))
        self.assertTrue(eligible_candidate(self.report, [self.record]))

    def test_known_digest_is_not_readapted_and_version_is_not_a_routing_gate(self):
        same = copy.deepcopy(self.report)
        same['identity']['apkSha256'] = self.record['identity']['apkSha256']
        self.assertFalse(eligible_candidate(same, [self.record]))

        older = copy.deepcopy(self.report)
        older['identity']['versionCode'] = 3040
        self.assertTrue(eligible_candidate(older, [self.record]))

    def test_identity_and_public_https_source_are_revalidated(self):
        for field, value in [('packageName', 'org.example.other'),
                             ('signerSha256', 'c' * 64), ('abi', 'x86_64')]:
            report = copy.deepcopy(self.report)
            report['identity'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                eligible_candidate(report, [self.record])
        report = copy.deepcopy(self.report)
        report['sourceUrl'] = 'http://example.invalid/candidate.apk'
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            eligible_candidate(report, [self.record])
        report['sourceUrl'] = 'https://example.invalid/candidate.apk?version=1'
        self.assertTrue(eligible_candidate(report, [self.record]))

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

    def test_gradle_validation_build_runs_fixed_release_gate_without_candidate_asset_mode(self):
        command = gradle_command('wechat')
        self.assertEqual([
            './gradlew', '--offline', '--no-daemon',
            ':app:testDebugUnitTest', ':app:testReleaseUnitTest',
            ':compat-core:test', ':compat-rules:test', ':compat-checker:test',
            ':app:verifyModuleMetadata', ':app:assembleDebug',
        ], command)
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
        class ReachedState(Exception):
            pass

        from scripts.test_candidate_selection import CandidateSelectionTest
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
                'apkFiles': [{'role': 'base', 'path': 'base.apk', 'sha256': accepted_digest}],
            }
            v2_bytes = b'newer accepted v2 apk fixture'
            v2_digest = hashlib.sha256(v2_bytes).hexdigest()
            v2_apk = cache / f'{v2_digest}.apk'
            v2_apk.write_bytes(v2_bytes)
            v2_case = {'recordId': 'accepted-v2-newer', 'kind': 'ACCEPTED_V2',
                'profileId': None, 'appId': 'wechat',
                'identity': dict(accepted['identity'], versionName='8.0.90', versionCode=3500,
                                 apkSha256=v2_digest),
                'apkFiles': [{'role': 'base', 'path': 'base.apk', 'sha256': v2_digest}],
                'source': dict(accepted['source'], apkFiles=[
                    {'role': 'base', 'path': 'base.apk', 'sha256': v2_digest}]),
                'acceptanceSnapshotSha256': '9' * 64}
            candidate_bytes = b'incompatible-candidate-fixture'
            candidate_apk = root / 'candidate.apk'
            candidate_apk.write_bytes(candidate_bytes)
            report = copy.deepcopy(self.report)
            candidate_digest = hashlib.sha256(candidate_bytes).hexdigest()
            report['identity']['apkSha256'] = candidate_digest
            report['apkFiles'] = [{'path': candidate_apk.name, 'role': 'base', 'sha256': candidate_digest}]
            semantic = CandidateSelectionTest._semantic_report('wechat', candidate_apk, 'NO_MATCH')
            semantic['identity'] = copy.deepcopy(report['identity'])
            semantic['inputHashes'][0]['path'] = candidate_apk.name
            static_path = root / 'static-check.json'
            static_path.write_text(json.dumps(semantic))
            from scripts.ci.candidate_selection import recognition_identity
            recognition, recognition_sha = recognition_identity(semantic)
            report.update(ruleCatalog=semantic['ruleCatalog'], snapshot=semantic['snapshot'],
                ruleCatalogSha256=semantic['ruleCatalog']['ruleCatalogSha256'],
                recognition=recognition, recognitionSha256=recognition_sha,
                diagnostic={'selection': 'NO_MATCH', 'diagnostics': {}, 'incompleteReasons': {}},
                staticReport=static_path.name,
                staticReportSha256=hashlib.sha256(static_path.read_bytes()).hexdigest())
            report_path = root / 'candidate-report.json'
            report_path.write_text(json.dumps(report))
            bundle = SimpleNamespace(historical_records=[accepted],
                profiles=[{'appId': 'wechat', 'historicalRecordId': accepted['recordId'],
                           'historicalAcceptanceSha256': self.profile['historicalAcceptanceSha256']}],
                history_sha256=self.bundle.history_sha256,
                expectations_sha256=self.bundle.expectations_sha256)
            (root / 'adaptation/checkout').mkdir(parents=True)

            adaptation_cases = [{'recordId': accepted['recordId'], 'profileId': self.profile['profileId'],
                'appId': 'wechat', 'identity': accepted['identity'], 'apkFiles': accepted['source']['apkFiles'],
                'source': accepted['source'], 'kind': 'MIGRATION',
                'acceptanceSnapshotSha256': self.profile['historicalAcceptanceSha256']}, v2_case]
            baseline = SimpleNamespace(migration_bundle=bundle, cases=adaptation_cases,
                app_id='wechat', v2_path=repository.resolve() / 'compatibility/wechat/acceptance-v2.json',
                sha256='3' * 64, v2_sha256=None,
                assert_unchanged=MagicMock())
            original_write_text = Path.write_text
            captured_state = {}

            def capture_state(path, data, *args, **kwargs):
                if path.name == 'state.json':
                    captured_state.update(json.loads(data))
                    raise ReachedState('state includes v2 case')
                return original_write_text(path, data, *args, **kwargs)

            with patch('scripts.ci.codex_adaptation._rules_acceptance',
                       return_value=(bundle, self.profile['ruleCatalog'], baseline)), \
                 patch('scripts.ci.codex_adaptation.eligible_candidate', return_value=True), \
                 patch('scripts.ci.codex_adaptation.git', side_effect=['base', 'agent-base']), \
                 patch('scripts.ci.codex_adaptation.subprocess.run'), \
                 patch('pathlib.Path.write_text', new=capture_state):
                with self.assertRaisesRegex(ReachedState, 'state includes v2 case'):
                    prepare_rules(repository, report_path, candidate_apk, root / 'adaptation')
            self.assertEqual([f'candidate-{candidate_digest}.apk',
                              f"{accepted['identity']['versionCode']}-{accepted_digest}.apk",
                              f'3500-{v2_digest}.apk'],
                             [Path(path).name for _, path, _ in captured_state['apks']])
            self.assertEqual([accepted['recordId'], 'accepted-v2-newer'],
                             captured_state['regressionRecordIds'])
            self.assertEqual('3' * 64, captured_state['baselineSha256'])

    def test_explicit_command_timeout_terminates_the_process_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(TimeoutError):
                run_bounded(['python3', '-c', 'import time; time.sleep(30)'],
                            Path(temporary), {}, Path(temporary) / 'timeout.log', timeout=0.1)


if __name__ == '__main__':
    unittest.main()
