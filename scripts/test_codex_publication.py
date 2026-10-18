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


class OrphanPublicationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.remote = root / 'remote.git'
        self.checkout = root / 'checkout'
        self.task = root / 'task'
        self.task.mkdir()
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], check=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.checkout)], check=True)
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.com')
        (self.checkout / 'profile.json').write_text('baseline\n')
        self.git('add', '.')
        self.git('commit', '-q', '-m', 'baseline')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.report = {
            'appId': 'qq', 'status': 'STATIC_VERIFIED_PENDING_RUNTIME',
            'identity': {'packageName': 'com.tencent.mobileqq', 'versionName': '9.3.70',
                         'versionCode': 16410, 'abi': 'arm64-v8a', 'apkSha256': 'a' * 64},
            'checkedVersions': ['9.3.70'],
            'sourceUrl': 'https://downv6.qq.com/qqweb/QQ_1/android_apk/qq.apk',
        }
        self.branch = 'adapt/qq-' + codex_task.candidate_key(self.report)
        (self.checkout / 'profile.json').write_text('baseline\ncandidate\n')
        (self.task / 'adaptation.patch').write_text(self.git('diff', '--binary', '--full-index') + '\n')
        self.git('add', '.')
        self.original_tree = self.git('write-tree')
        self.git('reset', '--hard', self.base)
        (self.task / 'state.json').write_text(json.dumps({'appId': 'qq', 'base': self.base}))
        (self.task / 'candidate-report.json').write_text(json.dumps(self.report))
        self.prs = []
        self.issue_body = codex_task.issue_body(self.report, 'INTERRUPTED',
                                               'https://github.com/NKU100/ImPad/actions/runs/5678')
        self.issue_open = True
        self.create_failure = ''
        self.operations = []
        self.url = 'https://github.com/NKU100/ImPad/pull/15'
        self.environment = {'GITHUB_WORKSPACE': str(self.checkout), 'GITHUB_OUTPUT': str(self.task / 'output')}

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.checkout, text=True).strip()

    def remote_head(self):
        return subprocess.check_output(['git', '--git-dir', str(self.remote), 'rev-parse',
                                        'refs/heads/' + self.branch], text=True).strip()

    def github(self, *args):
        self.operations.append(args)
        if args[:2] == ('pr', 'list'):
            return json.dumps(self.prs)
        if args[:2] == ('pr', 'view'):
            return json.dumps(self.prs[0])
        if args[0] == 'api' and '/git/ref/heads/main' in args[1]:
            return self.base
        if args[0] == 'api' and '/issues/11' in args[1]:
            return self.issue_body
        if args[:2] == ('issue', 'edit'):
            self.issue_body = Path(args[args.index('--body-file') + 1]).read_text()
            return ''
        if args[:2] == ('pr', 'create'):
            if self.create_failure == 'before-create':
                raise RuntimeError('PR creation unavailable')
            self.prs.append({'url': self.url, 'headRefName': self.branch,
                             'headRefOid': self.remote_head(), 'state': 'OPEN', 'baseRefName': 'main',
                             'author': {'login': 'app/github-actions', 'is_bot': True},
                             'body': Path(args[args.index('--body-file') + 1]).read_text(),
                             'isDraft': True, 'isCrossRepository': False})
            if self.create_failure == 'lost-response':
                raise RuntimeError('PR response lost')
            return self.url
        if args[:2] == ('pr', 'edit'):
            self.prs[0]['body'] = Path(args[args.index('--body-file') + 1]).read_text()
            self.prs[0]['headRefOid'] = self.remote_head()
            return ''
        if args[:2] == ('pr', 'ready'):
            self.prs[0]['isDraft'] = True
            return ''
        self.fail(f'Unexpected GitHub operation: {args}')

    def publish(self, issue, run_id, date):
        with patch('scripts.ci.codex_task.gh', side_effect=self.github), \
                patch.dict(os.environ, dict(self.environment, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)):
            codex_task.publish('NKU100/ImPad', self.task, issue,
                               'https://github.com/NKU100/ImPad/actions/runs/' + run_id, 'qq')

    def interrupted_publication(self, failure):
        self.create_failure = failure
        with self.assertRaisesRegex(RuntimeError, 'PR creation unavailable|PR response lost'):
            self.publish(0, '1234', '2026-10-18T01:00:00Z')
        previous_head = self.remote_head()
        self.git('switch', 'main')
        self.git('branch', '-D', self.branch)
        (self.task / 'resume-context.json').write_text(json.dumps({
            'checkpointRunId': '1234', 'checkpointArtifact': 'impad-qq-codex-adaptation',
            'publicationCheckpoints': [{'base': self.base, 'tree': self.original_tree}],
        }))
        # Owner continuation changes the validated patch, so recovery must use the saved checkpoint tree.
        (self.checkout / 'profile.json').write_text('baseline\ncandidate\nowner correction\n')
        (self.task / 'adaptation.patch').write_text(self.git('diff', '--binary', '--full-index') + '\n')
        self.git('checkout', '--', 'profile.json')
        self.create_failure = ''
        return previous_head

    def test_resume_reuses_branch_left_before_pr_creation(self):
        self.interrupted_publication('before-create')
        self.publish(11, '5679', '2026-10-18T02:00:00Z')
        self.assertEqual(1, len(self.prs))
        self.assertIn('tracks #11', self.prs[0]['body'])
        self.assertIn('Adaptation PR: ' + self.url, self.issue_body)
        self.assertIn('PR head: `' + self.remote_head() + '`', self.issue_body)
        self.assertEqual('baseline\ncandidate\nowner correction', self.git('show', 'HEAD:profile.json'))

    def test_resume_recovers_pr_whose_create_response_was_lost(self):
        previous_head = self.interrupted_publication('lost-response')
        self.publish(11, '5679', '2026-10-18T02:00:00Z')
        self.assertEqual(1, len(self.prs), 'Recovery must not create a duplicate PR')
        self.assertNotEqual(previous_head, self.remote_head())
        self.assertIn('tracks #11', self.prs[0]['body'])
        self.assertIn('Status: `WAITING_RUNTIME`', self.issue_body)

    def test_recovery_refuses_changed_or_unmanaged_pr_before_mutations(self):
        for changes in (
            {'author': {'login': 'outsider', 'is_bot': False}},
            {'baseRefName': 'other'}, {'headRefName': 'adapt/other'},
            {'headRefOid': 'b' * 40}, {'state': 'CLOSED'},
            {'body': 'tracks #99'}, {'isCrossRepository': True},
        ):
            with self.subTest(changes=changes):
                fixture = OrphanPublicationTest()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                previous_head = fixture.interrupted_publication('lost-response')
                fixture.prs[0].update(changes)
                fixture.operations = []
                with self.assertRaises(ValueError):
                    fixture.publish(11, '5679', '2026-10-18T02:00:00Z')
                self.assertEqual(previous_head, fixture.remote_head())
                self.assertFalse(any(op[:2] in {('pr', 'edit'), ('pr', 'create'), ('pr', 'ready'),
                                               ('issue', 'edit')} for op in fixture.operations))

    def test_recovery_refuses_branch_with_different_checkpoint_tree(self):
        previous_head = self.interrupted_publication('before-create')
        context = json.loads((self.task / 'resume-context.json').read_text())
        context['publicationCheckpoints'][0]['tree'] = self.git('rev-parse', self.base + '^{tree}')
        (self.task / 'resume-context.json').write_text(json.dumps(context))
        with self.assertRaises(ValueError):
            self.publish(11, '5679', '2026-10-18T02:00:00Z')
        self.assertEqual(previous_head, self.remote_head())
        self.assertEqual([], self.prs)

    def test_recovered_ready_pr_draft_failure_stops_before_push_and_linking(self):
        previous_head = self.interrupted_publication('lost-response')
        self.prs[0]['isDraft'] = False
        self.operations = []
        original_github = self.github
        def github(*args):
            if args[:2] == ('pr', 'ready'):
                raise RuntimeError('Draft conversion failed')
            return original_github(*args)
        with patch.object(self, 'github', side_effect=github):
            with self.assertRaisesRegex(RuntimeError, 'Draft conversion failed'):
                self.publish(11, '5679', '2026-10-18T02:00:00Z')
        self.assertEqual(previous_head, self.remote_head())
        self.assertEqual('main', self.git('branch', '--show-current'))
        self.assertFalse(any(op[:2] in {('pr', 'edit'), ('issue', 'edit')} for op in self.operations))

    def test_recovery_lease_refuses_concurrent_branch_update(self):
        previous_head = self.interrupted_publication('before-create')
        real_run = subprocess.run
        racer_head = []
        def run(command, *args, **kwargs):
            if command[:2] == ['git', 'push']:
                commit = subprocess.check_output(['git', 'commit-tree', self.original_tree, '-p', previous_head,
                                                  '-m', 'concurrent update'], cwd=self.checkout, text=True).strip()
                real_run(['git', 'push', 'origin', commit + ':refs/heads/' + self.branch],
                         cwd=self.checkout, check=True)
                racer_head.append(commit)
            return real_run(command, *args, **kwargs)
        with patch('scripts.ci.codex_task.subprocess.run', side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                self.publish(11, '5679', '2026-10-18T02:00:00Z')
        self.assertEqual(racer_head[0], self.remote_head())
        self.assertIn('PR head: `' + previous_head + '`', self.issue_body)
        self.assertIn('Status: `INTERRUPTED`', self.issue_body)
        self.assertEqual(1, len(self.prs))

    def test_pr_recovery_keeps_original_run_after_checkpoint_pointer_advances(self):
        self.interrupted_publication('lost-response')
        context = json.loads((self.task / 'resume-context.json').read_text())
        context['publicationCheckpoints'][0]['runId'] = '1234'
        context['checkpointRunId'] = '5678'
        (self.task / 'resume-context.json').write_text(json.dumps(context))
        self.publish(11, '5679', '2026-10-18T02:00:00Z')
        self.assertEqual(1, len(self.prs))
        self.assertIn('tracks #11', self.prs[0]['body'])
