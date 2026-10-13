import copy
import unittest
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch
from scripts.ci.runtime_task import runtime_result, main, close_issue_if_complete


class RuntimeTaskTest(unittest.TestCase):
    def test_only_runtime_verified_closes_the_adaptation_issue(self):
        with patch('scripts.ci.runtime_task.gh') as github:
            close_issue_if_complete('owner/repo', 8, 'RUNTIME_REJECTED')
            github.assert_not_called()
            close_issue_if_complete('owner/repo', 8, 'RUNTIME_VERIFIED')
        github.assert_called_once_with('issue', 'close', '8', '--repo', 'owner/repo')

    def setUp(self):
        identity = {'packageName': 'com.tencent.mm', 'versionName': '8.0.79', 'versionCode': 3200,
                    'abi': 'arm64-v8a', 'apkSha256': 'a' * 64,
                    'signerSha256': '0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c'}
        self.report = {'appId': 'wechat', 'status': 'STATIC_VERIFIED_PENDING_RUNTIME',
                       'identity': identity,
                       'suggestedProfile': {'identity': identity,
                                            'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/test.apk',
                                            'hooks': [{'id': 'tablet'}, {'id': 'login'}],
                                            'verificationStatus': 'static-verified'}}
        self.report['sourceUrl'] = 'https://dldir1v6.qq.com/weixin/android/test.apk'
        self.smoke = {'appId': 'wechat', 'packageName': 'com.tencent.mm', 'abi': 'arm64-v8a',
                      'status': 'RUNTIME_SMOKE_VERIFIED', 'baseline': 'NO_TABLET_ENTRY',
                      'hooks': 2, 'hookIds': ['tablet', 'login'], 'qrPage': 'LoginAsExDeviceUI',
                      'apkSha256': 'a' * 64}

    def test_success_promotes_only_matching_candidate_and_preserves_input(self):
        original = copy.deepcopy(self.report)
        result = runtime_result(self.report, self.smoke, 'success')
        self.assertEqual(result['status'], 'RUNTIME_VERIFIED')
        self.assertEqual(result['suggestedProfile']['verificationStatus'], 'runtime-verified-hosted')
        self.assertEqual(self.report, original)

    def test_failure_or_missing_evidence_cannot_promote(self):
        for evidence, conclusion in [(self.smoke, 'failure'), ({}, 'success'),
                                      (dict(self.smoke, qrPage='Splash'), 'success'),
                                      (dict(self.smoke, apkSha256='b' * 64), 'success')]:
            with self.subTest(evidence=evidence, conclusion=conclusion):
                result = runtime_result(self.report, evidence, conclusion)
                self.assertEqual(result['status'], 'RUNTIME_REJECTED')
                self.assertEqual(result['suggestedProfile']['verificationStatus'], 'static-verified')

    def test_rejects_cross_app_package_abi_or_hook_evidence(self):
        for field, value in [('appId', 'other'), ('packageName', 'org.example.other'),
                             ('abi', 'x86_64'), ('hookIds', ['tablet'])]:
            with self.subTest(field=field):
                result = runtime_result(self.report, dict(self.smoke, **{field: value}), 'success')
                self.assertEqual(result['status'], 'RUNTIME_REJECTED')

    def test_cancelled_or_skipped_smoke_preserves_pending_runtime_status(self):
        for conclusion in ['cancelled', 'skipped']:
            for smoke in [{}, self.smoke]:
                with self.subTest(conclusion=conclusion, smoke=smoke):
                    result = runtime_result(self.report, smoke, conclusion)
                    self.assertEqual(result['status'], 'STATIC_VERIFIED_PENDING_RUNTIME')
                    self.assertEqual(result['suggestedProfile']['verificationStatus'], 'static-verified')
                    self.assertEqual(result['runtimeConclusion'], conclusion)

    def test_cancelled_acceptance_writes_pending_report_without_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'candidate-report.json').write_text(json.dumps(self.report))
            pr = {'headRefOid': 'tested', 'headRefName': 'adapt/test', 'state': 'OPEN', 'body': ''}
            def github(*args):
                if args[:2] == ('pr', 'view'):
                    return json.dumps(pr)
                self.fail('An inconclusive run must not reject or promote the PR')
            env = {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '1',
                   'GITHUB_OUTPUT': str(root / 'output'), 'GITHUB_STEP_SUMMARY': str(root / 'summary')}
            with patch.dict(os.environ, env), patch('sys.argv', ['runtime_task', '--directory', str(root),
                      '--smoke', str(root / 'missing.json'), '--conclusion', 'cancelled', '--pr', '1',
                      '--head', 'tested', '--issue', '2']), patch('scripts.ci.runtime_task.gh', side_effect=github), patch('scripts.ci.runtime_task.write_issue'), patch('scripts.ci.runtime_task.publish_outcome'):
                main()
            result = json.loads((root / 'runtime-candidate-report.json').read_text())
            self.assertEqual(result['status'], 'STATIC_VERIFIED_PENDING_RUNTIME')
            self.assertIn('pipeline_status=STATIC_VERIFIED_PENDING_RUNTIME', (root / 'output').read_text())

    def test_publication_changes_only_manifest_on_the_tested_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            origin = root / 'remote.git'
            checkout = root / 'checkout'
            subprocess.run(['git', 'init', '--bare', '-q', str(origin)], check=True)
            subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=checkout).decode().strip()
            git('config', 'user.name', 'test')
            git('config', 'user.email', 'test@example.com')
            git('remote', 'add', 'origin', str(origin))
            (checkout / 'compatibility/wechat').mkdir(parents=True)
            baseline_identity = dict(self.report['identity'], versionName='8.0.69', versionCode=3040,
                                     apkSha256='b' * 64)
            before = {'identity': baseline_identity,
                      'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/baseline.apk',
                      'hooks': [{'id': 'tablet'}, {'id': 'login'}],
                      'verificationStatus': 'runtime-verified-local'}
            manifest = checkout / 'compatibility/wechat/targets.json'
            manifest.write_text(json.dumps([before, self.report['suggestedProfile']]))
            (checkout / 'code.txt').write_text('tested module code')
            git('add', '.')
            git('commit', '-qm', 'test candidate')
            head = git('rev-parse', 'HEAD')
            git('push', '-q', 'origin', 'HEAD:refs/heads/adapt/test')
            directory = checkout / 'result'
            directory.mkdir()
            (directory / 'candidate-report.json').write_text(json.dumps(self.report))
            evidence = directory / 'smoke.json'
            evidence.write_text(json.dumps(self.smoke))
            env = {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '1',
                   'GITHUB_OUTPUT': str(root / 'output'), 'GITHUB_STEP_SUMMARY': str(root / 'summary')}
            pr = {'headRefOid': head, 'headRefName': 'adapt/test', 'state': 'OPEN', 'body': 'Static checks passed'}
            previous = Path.cwd()
            try:
                os.chdir(checkout)
                with patch.dict(os.environ, env), patch('sys.argv', ['runtime_task', '--directory', str(directory),
                           '--smoke', str(evidence), '--conclusion', 'success', '--pr', 'https://github.com/owner/repo/pull/1',
                           '--head', head, '--issue', '2']), patch('scripts.ci.runtime_task.gh', side_effect=[json.dumps(pr), '', '', '', '']), patch('scripts.ci.runtime_task.write_issue'), patch('scripts.ci.runtime_task.publish_outcome'):
                    main()
            finally:
                os.chdir(previous)
            git('fetch', '-q', 'origin', 'adapt/test')
            promoted = json.loads(git('show', 'FETCH_HEAD:compatibility/wechat/targets.json'))
            self.assertEqual(promoted[0], before)
            self.assertEqual(promoted[1]['verificationStatus'], 'runtime-verified-hosted')
            self.assertEqual(git('diff', '--name-only', head, 'FETCH_HEAD'), 'compatibility/wechat/targets.json')
            self.assertEqual(git('rev-parse', 'FETCH_HEAD^'), head)

    def test_changed_pr_head_cannot_be_promoted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'candidate-report.json').write_text(json.dumps(self.report))
            (root / 'smoke.json').write_text(json.dumps(self.smoke))
            pr = {'headRefOid': 'changed', 'headRefName': 'adapt/test', 'state': 'OPEN', 'body': ''}
            env = {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '1'}
            with patch.dict(os.environ, env), patch('sys.argv', ['runtime_task', '--directory', str(root),
                      '--smoke', str(root / 'smoke.json'), '--conclusion', 'success', '--pr', '1',
                      '--head', 'tested', '--issue', '2']), patch('scripts.ci.runtime_task.gh', return_value=json.dumps(pr)), patch('scripts.ci.runtime_task.subprocess.run') as git:
                with self.assertRaisesRegex(ValueError, 'changed or closed'):
                    main()
                git.assert_not_called()

    def test_requires_static_verification(self):
        self.report['status'] = 'NEEDS_HOOK_REVIEW'
        with self.assertRaises(ValueError):
            runtime_result(self.report, self.smoke, 'success')
