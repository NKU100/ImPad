import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.ci import codex_task


class CodexPublicationTest(unittest.TestCase):
    def test_managed_author_accepts_github_app_identity_only_when_bot(self):
        self.assertTrue(hasattr(codex_task, 'managed_pr_author'))
        for author, accepted in (
            ({'login': 'app/github-actions', 'is_bot': True}, True),
            ({'login': 'github-actions[bot]', 'is_bot': True}, True),
            ({'login': 'NKU100', 'is_bot': False}, True),
            ({'login': 'app/github-actions', 'is_bot': False}, False),
            ({'login': 'github-actions[bot]'}, False),
            ({'login': 'app/other-app', 'is_bot': True}, False),
            ({'login': 'other-user', 'is_bot': False}, False),
        ):
            with self.subTest(author=author):
                self.assertEqual(accepted, codex_task.managed_pr_author(author, 'NKU100'))

    def test_publication_updates_existing_app_authored_pr_and_keeps_it_draft(self):
        for initially_draft, draft_failure in ((True, False), (False, False), (False, True)):
            with self.subTest(initially_draft=initially_draft, draft_failure=draft_failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                remote = root / 'remote.git'; checkout = root / 'checkout'; task = root / 'task'
                task.mkdir()
                def git(*args):
                    return subprocess.check_output(['git', *args], cwd=checkout, text=True).strip()
                subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
                subprocess.run(['git', 'init', '-q', '-b', 'main', str(checkout)], check=True)
                git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.com')
                (checkout / 'profile.json').write_text('baseline\n')
                git('add', '.'); git('commit', '-q', '-m', 'baseline')
                base = git('rev-parse', 'HEAD')
                git('remote', 'add', 'origin', str(remote))
                report = {
                    'appId': 'qq', 'status': 'STATIC_VERIFIED_PENDING_RUNTIME',
                    'identity': {'packageName': 'com.tencent.mobileqq', 'versionName': '9.3.70',
                                 'versionCode': 16410, 'abi': 'arm64-v8a', 'apkSha256': 'a' * 64},
                    'checkedVersions': ['9.3.70', '9.3.1'],
                    'sourceUrl': 'https://downv6.qq.com/qqweb/QQ_1/android_apk/qq.apk',
                }
                branch = 'adapt/qq-' + codex_task.candidate_key(report)
                git('push', '-q', 'origin', 'HEAD:refs/heads/' + branch)
                (checkout / 'profile.json').write_text('baseline\ncandidate\n')
                (task / 'adaptation.patch').write_text(git('diff') + '\n')
                git('checkout', '--', 'profile.json')
                (task / 'state.json').write_text(json.dumps({'appId': 'qq', 'base': base}))
                (task / 'candidate-report.json').write_text(json.dumps(report))
                url = 'https://github.com/NKU100/ImPad/pull/15'
                (task / 'resume-context.json').write_text(json.dumps({'existingPr': url, 'existingPrHead': base}))
                pr = {'headRefName': branch, 'headRefOid': base, 'state': 'OPEN', 'baseRefName': 'main',
                      'author': {'login': 'app/github-actions', 'is_bot': True},
                      'body': 'tracks #14', 'isDraft': initially_draft}
                ready_calls = []

                real_run = subprocess.run
                def run(command, *args, **kwargs):
                    if command[:2] == ['git', 'push']:
                        self.assertTrue(pr['isDraft'], 'PR must be draft before pushing')
                    return real_run(command, *args, **kwargs)

                def github(*args):
                    if args[:2] == ('pr', 'view'):
                        return json.dumps(pr)
                    if args[0] == 'api' and '/git/ref/heads/main' in args[1]:
                        return base
                    if args[:2] == ('pr', 'ready'):
                        self.assertIn('--undo', args)
                        ready_calls.append(args)
                        if draft_failure:
                            raise RuntimeError('Draft conversion failed')
                        pr['isDraft'] = True
                    elif args[:2] == ('pr', 'edit'):
                        self.assertFalse(draft_failure, 'Failed draft conversion must stop editing')
                        if '--draft' in args:
                            raise ValueError('unknown flag: --draft')
                        pr['body'] = Path(args[args.index('--body-file') + 1]).read_text()
                    elif args[:2] == ('issue', 'edit') or args[0] == 'api':
                        return 'previous result'
                    else:
                        self.fail(f'Unexpected GitHub operation: {args}')
                    return ''

                with patch('scripts.ci.codex_task.gh', side_effect=github), \
                        patch('scripts.ci.codex_task.subprocess.run', side_effect=run), \
                        patch.dict(os.environ, {'GITHUB_WORKSPACE': str(checkout), 'GITHUB_OUTPUT': str(task / 'output')}):
                    if draft_failure:
                        with self.assertRaisesRegex(RuntimeError, 'Draft conversion failed'):
                            codex_task.publish('NKU100/ImPad', task, 14,
                                               'https://github.com/NKU100/ImPad/actions/runs/1234', 'qq')
                    else:
                        codex_task.publish('NKU100/ImPad', task, 14,
                                           'https://github.com/NKU100/ImPad/actions/runs/1234', 'qq')
                self.assertEqual(0 if initially_draft else 1, len(ready_calls))
                remote_head = subprocess.check_output(['git', '--git-dir', str(remote), 'rev-parse',
                                                        'refs/heads/' + branch], text=True).strip()
                if draft_failure:
                    self.assertEqual(base, remote_head)
                    self.assertEqual('main', git('branch', '--show-current'))
                    self.assertEqual('tracks #14', pr['body'])
                    continue
                published = subprocess.check_output(['git', '--git-dir', str(remote), 'show',
                                                      'refs/heads/' + branch + ':profile.json'], text=True)
                self.assertEqual('baseline\ncandidate\n', published)
                self.assertTrue(pr['isDraft'])
                self.assertIn('tracks #14', pr['body'])
                self.assertIn('pr_url=' + url, (task / 'output').read_text())
