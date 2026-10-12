import unittest
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from scripts.ci.codex_task import (existing_attempt, pending_attempt, select_candidate_report,
                                   claim, checkpoint_reference, record_checkpoint)
from scripts.ci.codex_adaptation import candidate_key


def record(status='NEEDS_HOOK_REVIEW', label='impad-adaptation', marker='impad-adaptation'):
    return {'number': 3, 'state': 'open', 'user': {'login': 'github-actions[bot]', 'type': 'Bot'},
            'labels': [{'name': label}],
            'body': f'<!-- {marker}:3200-abc -->\nStatus: `{status}`'}


def candidate_report():
    return {'appId': 'wechat', 'identity': {
        'packageName': 'com.tencent.mm', 'abi': 'arm64-v8a', 'versionName': '8.0.79',
        'versionCode': 3200, 'apkSha256': 'a' * 64,
    }, 'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/wechat.apk'}


class CodexTaskTest(unittest.TestCase):
    def test_checkpoint_reference_reads_valid_run_and_artifact_from_issue(self):
        body = ('Status: `ADAPTATION_RUNNING`\n'
                'Checkpoint run: https://github.com/owner/repo/actions/runs/1234\n'
                'Checkpoint artifact: `impad-wechat-codex-adaptation`\n')
        self.assertEqual(('1234', 'impad-wechat-codex-adaptation'),
                         checkpoint_reference(body, 'owner/repo', 'wechat'))

    def test_pending_attempt_pins_oldest_unfinished_issue_for_the_selected_app(self):
        older = record('RUNTIME_REJECTED')
        older.update(number=8, created_at='2026-10-01T00:00:00Z')
        older['body'] = older['body'].replace('impad-adaptation:3200-abc', 'impad-adaptation:wecom-com.tencent.wework-arm64-v8a-83835-' + 'a' * 64)
        newer = dict(older, number=9, created_at='2026-10-02T00:00:00Z')
        complete = dict(older, number=7, created_at='2026-09-30T00:00:00Z')
        complete['body'] = complete['body'].replace('RUNTIME_REJECTED', 'RUNTIME_VERIFIED')
        other_app = dict(older, number=6, created_at='2026-09-29T00:00:00Z')
        other_app['body'] = other_app['body'].replace('wecom-com.tencent.wework', 'wechat-com.tencent.mm')

        self.assertEqual(8, pending_attempt([newer, complete, other_app, older], 'wecom')['number'])

    def test_candidate_report_selection_uses_active_checkpoint_when_latest_changed(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            current = dict(candidate_report(), identity=dict(candidate_report()['identity'],
                           versionName='8.0.80', versionCode=3201, apkSha256='b' * 64))
            checkpoint = candidate_report()
            current_path = directory / 'latest.json'
            current_path.write_text(json.dumps(current))
            checkpoint_directory = directory / 'checkpoint'
            checkpoint_directory.mkdir()
            checkpoint_path = checkpoint_directory / 'candidate-report.json'
            checkpoint_path.write_text(json.dumps(checkpoint))
            output = directory / 'selected.json'

            selected = select_candidate_report(current_path, checkpoint_directory,
                                               candidate_key(checkpoint), output)

            self.assertEqual(candidate_key(checkpoint), candidate_key(selected))
            self.assertEqual(candidate_key(checkpoint), candidate_key(json.loads(output.read_text())))

    def test_candidate_report_selection_fails_closed_when_pinned_report_is_missing(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            current_path = directory / 'latest.json'
            current_path.write_text(json.dumps(candidate_report()))
            with self.assertRaisesRegex(ValueError, 'matches the active adaptation checkpoint'):
                select_candidate_report(current_path, directory / 'missing', 'wecom-other-candidate',
                                        directory / 'selected.json')

    def test_checkpoint_record_persists_current_run_pr_and_validation_stage(self):
        report = dict(candidate_report(), status='STATIC_VERIFIED_PENDING_RUNTIME')
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            report_path = directory / 'candidate-report.json'
            report_path.write_text(json.dumps(report))
            (directory / 'checkpoint.json').write_text(json.dumps({
                'schemaVersion': 1, 'candidateKey': candidate_key(report),
                'stage': 'static-verified-pending-runtime',
            }))
            output = directory / 'output'
            issue_body = ('Adaptation PR: https://github.com/NKU100/ImPad/pull/17\n'
                          'PR head: `' + 'c' * 40 + '`\n')
            with patch('scripts.ci.codex_task.gh', return_value=issue_body), \
                    patch('scripts.ci.codex_task.write_issue') as write_issue_mock, \
                    patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
                record_checkpoint('NKU100/ImPad', report_path, directory, 8,
                                  'https://github.com/NKU100/ImPad/actions/runs/1234', 'wechat')
            body = write_issue_mock.call_args.args[2]
            self.assertIn('Status: `WAITING_RUNTIME`', body)
            self.assertIn('Checkpoint run: https://github.com/NKU100/ImPad/actions/runs/1234', body)
            self.assertIn('Checkpoint artifact: `impad-wechat-codex-adaptation`', body)
            self.assertIn('Adaptation PR: https://github.com/NKU100/ImPad/pull/17', body)
            self.assertIn('PR head: `' + 'c' * 40 + '`', body)
            self.assertIn('checkpoint_saved=true', output.read_text())

    def test_claim_automatically_resumes_checkpoint_and_keeps_existing_pr(self):
        issue = record('RUNTIME_REJECTED')
        issue['body'] += ('\nCheckpoint run: https://github.com/owner/repo/actions/runs/1234\n'
                          'Checkpoint artifact: `impad-wechat-codex-adaptation`\n'
                          'Adaptation PR: https://github.com/owner/repo/pull/17\n')
        calls = []
        edited_body = []

        def fake_gh(*args):
            calls.append(args)
            if args[:2] == ('api', '--paginate'):
                return json.dumps([[issue]])
            if args[:2] == ('issue', 'edit') and '--body-file' in args:
                edited_body.append(Path(args[args.index('--body-file') + 1]).read_text())
            return ''

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'output'
            with patch('scripts.ci.codex_task.eligible_candidate', return_value=True), \
                    patch('scripts.ci.codex_task.candidate_key', return_value='3200-abc'), \
                    patch('scripts.ci.codex_task.gh', side_effect=fake_gh), \
                    patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
                claim('owner/repo', candidate_report(), [], Path(root),
                      'https://github.com/owner/repo/actions/runs/5678')

            values = dict(line.split('=', 1) for line in output.read_text().splitlines())
            resume = json.loads((Path(root) / 'resume-context.json').read_text())

        self.assertEqual('true', values['claimed'])
        self.assertEqual('1234', values['checkpoint_run_id'])
        self.assertEqual('impad-wechat-codex-adaptation', values['checkpoint_artifact'])
        self.assertEqual('https://github.com/owner/repo/pull/17', values['existing_pr'])
        self.assertEqual('RUNTIME_REJECTED', resume['status'])
        self.assertIn('Checkpoint run: https://github.com/owner/repo/actions/runs/1234', edited_body[0])
        self.assertIn('Adaptation PR: https://github.com/owner/repo/pull/17', edited_body[0])

    def test_runtime_verified_issue_is_the_only_completed_record(self):
        issue = record('RUNTIME_VERIFIED')
        issue['state'] = 'closed'
        calls = []
        output_text = ''

        def fake_gh(*args):
            calls.append(args)
            return json.dumps([[issue]]) if args[:2] == ('api', '--paginate') else ''

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'output'
            with patch('scripts.ci.codex_task.eligible_candidate', return_value=True), \
                    patch('scripts.ci.codex_task.candidate_key', return_value='3200-abc'), \
                    patch('scripts.ci.codex_task.gh', side_effect=fake_gh), \
                    patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
                claim('owner/repo', candidate_report(), [], Path(root),
                      'https://github.com/owner/repo/actions/runs/5678')
            output_text = output.read_text()

        self.assertIn('claimed=false', output_text)
        self.assertFalse(any(call[:2] == ('issue', 'reopen') for call in calls))

    def test_open_runtime_verified_issue_is_closed_and_not_restarted(self):
        issue = record('RUNTIME_VERIFIED')
        issue['state'] = 'open'
        calls = []
        def fake_gh(*args):
            calls.append(args)
            return json.dumps([[issue]]) if args[:2] == ('api', '--paginate') else ''
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'output'
            with patch('scripts.ci.codex_task.eligible_candidate', return_value=True), \
                    patch('scripts.ci.codex_task.candidate_key', return_value='3200-abc'), \
                    patch('scripts.ci.codex_task.gh', side_effect=fake_gh), \
                    patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
                claim('owner/repo', candidate_report(), [], Path(root), 'run')
            output_text = output.read_text()
        self.assertIn('claimed=false', output_text)
        self.assertTrue(any(call[:2] == ('issue', 'close') for call in calls))
        self.assertFalse(any(call[:2] == ('issue', 'edit') for call in calls))

    def test_issue_copy_uses_the_selected_application_display_name(self):
        from scripts.ci import codex_task
        from scripts.ci import app_policy
        policy = replace(codex_task.get_policy(), app_id='qq', display_name='QQ')
        report = {'appId': 'qq', 'identity': {'packageName': policy.package_name, 'abi': policy.supported_abis[0],
                                                'versionName': '9.1', 'versionCode': 91, 'apkSha256': 'a' * 64},
                  'sourceUrl': 'https://example.invalid/qq.apk'}
        with patch.dict(app_policy.POLICIES, {'qq': policy}):
            body = codex_task.issue_body(report, 'ADAPTATION_RUNNING', 'https://example.invalid/run')
        self.assertIn('QQ 9.1 (91)', body)
        self.assertNotIn('WeChat 9.1', body)

    def test_closed_unfinished_attempt_starts_a_new_record(self):
        issue = record()
        issue['state'] = 'closed'
        calls=[]
        with tempfile.TemporaryDirectory() as root:
            def fake_gh(*args):
                calls.append(args)
                if args[:2] == ('api', '--paginate'):
                    return json.dumps([[issue]])
                if args[:2] == ('issue', 'create'):
                    return 'https://github.com/owner/repo/issues/4'
                return ''
            with patch('scripts.ci.codex_task.eligible_candidate', return_value=True), patch('scripts.ci.codex_task.candidate_key',return_value='3200-abc'), patch('scripts.ci.codex_task.gh',side_effect=fake_gh), patch.dict(os.environ,{'GITHUB_OUTPUT':str(Path(root)/'output')}):
                claim('owner/repo',candidate_report(),[],Path(root),'run')
            self.assertFalse(any(c[:2] == ('issue','reopen') for c in calls))

    def test_legacy_label_and_marker_still_match_the_full_candidate_key(self):
        issue = record(label='wechatpad-adaptation', marker='wechatpad-adaptation')
        self.assertEqual(issue, existing_attempt([issue], '3200-abc'))
        self.assertIsNone(existing_attempt([issue], '3200-ab'))

    def test_untrusted_author_missing_label_and_pr_are_ignored(self):
        for change in [{'user': {'login': 'stranger', 'type': 'User'}}, {'labels': []}, {'pull_request': {}}]:
            issue = record()
            issue.update(change)
            self.assertIsNone(existing_attempt([issue], '3200-abc'))

    def test_other_build_or_partial_hash_does_not_block_candidate(self):
        self.assertIsNone(existing_attempt([record()], '3200-ab'))

    def test_legacy_attempt_is_automatically_reused(self):
        issue = record(label='wechatpad-adaptation', marker='wechatpad-adaptation')
        calls = []
        def fake_gh(*args):
            calls.append(args)
            return json.dumps([[issue]]) if args[:2] == ('api', '--paginate') else ''
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'output'
            with patch('scripts.ci.codex_task.eligible_candidate', return_value=True), \
                    patch('scripts.ci.codex_task.candidate_key', return_value='3200-abc'), \
                    patch('scripts.ci.codex_task.gh', side_effect=fake_gh), \
                    patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
                claim('owner/repo', candidate_report(), [], Path(root), 'run')
            self.assertIn('claimed=true', output.read_text())
            self.assertIn('issue=3', output.read_text())
            self.assertFalse(any(call[:2] == ('issue', 'create') for call in calls))

    def test_pending_runtime_attempt_is_automatically_resumed(self):
        issue = record(status='WAITING_RUNTIME', label='wechatpad-adaptation', marker='wechatpad-adaptation')
        issue['body'] += '\nBlocker: hosted UI differs at account switch.\n'
        calls = []
        def fake_gh(*args):
            calls.append(args)
            return json.dumps([[issue]]) if args[:2] == ('api', '--paginate') else ''
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'output'
            with patch('scripts.ci.codex_task.eligible_candidate', return_value=True), \
                    patch('scripts.ci.codex_task.candidate_key', return_value='3200-abc'), \
                    patch('scripts.ci.codex_task.gh', side_effect=fake_gh), \
                    patch('scripts.ci.codex_task.issue_body', return_value='running'), \
                    patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
                claim('owner/repo', candidate_report(), [], Path(root), 'run')
            self.assertIn('claimed=true', output.read_text())
            self.assertTrue(any(call[:2] == ('issue', 'edit') for call in calls))
            self.assertFalse(any(call[:2] == ('issue', 'create') for call in calls))
            resume = json.loads((Path(root) / 'resume-context.json').read_text())
            self.assertIn('hosted UI differs at account switch.', resume['previousBlockers'])

    def test_renamed_repo_restores_checkpoint_pointer_from_legacy_run_url(self):
        issue = record(status='ADAPTATION_RUNNING', label='wechatpad-adaptation', marker='wechatpad-adaptation')
        issue['body'] += '\nRun: https://github.com/NKU100/WeChatPad/actions/runs/12345\n'
        calls = []
        def fake_gh(*args):
            calls.append(args)
            if args[:2] == ('api', '--paginate'):
                return json.dumps([[issue]])
            return ''
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'output'
            with patch('scripts.ci.codex_task.eligible_candidate', return_value=True), \
                    patch('scripts.ci.codex_task.candidate_key', return_value='3200-abc'), \
                    patch('scripts.ci.codex_task.gh', side_effect=fake_gh), \
                    patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
                claim('NKU100/ImPad', candidate_report(), [], Path(root), 'run')
            values = dict(line.split('=', 1) for line in output.read_text().splitlines())
            self.assertEqual('12345', values['checkpoint_run_id'])
            self.assertEqual('impad-wechat-codex-adaptation', values['checkpoint_artifact'])
            self.assertFalse(any(call[:2] == ('issue', 'reopen') for call in calls))
            self.assertFalse(any(call[:2] == ('issue', 'create') for call in calls))

    def test_checkpoint_from_unrelated_repository_is_ignored_but_candidate_continues(self):
        issue = record(status='ADAPTATION_RUNNING', label='wechatpad-adaptation', marker='wechatpad-adaptation')
        issue['body'] += '\nRun: https://github.com/Other/WeChatPad/actions/runs/12345\n'
        calls = []
        def fake_gh(*args):
            calls.append(args)
            return json.dumps([[issue]]) if args[:2] == ('api', '--paginate') else ''
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'output'
            with patch('scripts.ci.codex_task.eligible_candidate', return_value=True), \
                    patch('scripts.ci.codex_task.candidate_key', return_value='3200-abc'), \
                    patch('scripts.ci.codex_task.gh', side_effect=fake_gh), \
                    patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
                claim('NKU100/ImPad', candidate_report(), [], Path(root), 'run')
            values = dict(line.split('=', 1) for line in output.read_text().splitlines())
            self.assertEqual('true', values['claimed'])
            self.assertEqual('', values['checkpoint_run_id'])
            self.assertFalse(any(call[:2] == ('api', 'repos/NKU100/ImPad/actions/runs/12345') for call in calls))
            self.assertFalse(any(call[:2] == ('issue', 'create') for call in calls))

    def test_conflicting_legacy_and_current_records_fail_closed(self):
        issues = [record(), record(label='wechatpad-adaptation', marker='wechatpad-adaptation')]
        calls = []
        def fake_gh(*args):
            calls.append(args)
            return json.dumps([issues]) if args[:2] == ('api', '--paginate') else ''
        with tempfile.TemporaryDirectory() as root:
            with patch('scripts.ci.codex_task.eligible_candidate', return_value=True), \
                    patch('scripts.ci.codex_task.candidate_key', return_value='3200-abc'), \
                    patch('scripts.ci.codex_task.gh', side_effect=fake_gh), \
                    patch.dict(os.environ, {'GITHUB_OUTPUT': str(Path(root) / 'output')}), \
                    self.assertRaisesRegex(ValueError, 'Multiple trusted adaptation records'):
                claim('owner/repo', candidate_report(), [], Path(root), 'run')
        self.assertFalse(any(call[:2] == ('issue', 'create') or call[:2] == ('label', 'create') for call in calls))

    def test_pending_pr_is_preserved_for_automatic_continuation(self):
        issue = record('WAITING_RUNTIME')
        issue['body'] += '\nAdaptation PR: https://github.com/owner/repo/pull/7'
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'output'
            def fake_gh(*args):
                return json.dumps([[issue]]) if args[:2] == ('api', '--paginate') else ''
            with patch('scripts.ci.codex_task.eligible_candidate', return_value=True), patch('scripts.ci.codex_task.candidate_key', return_value='3200-abc'), patch('scripts.ci.codex_task.gh', side_effect=fake_gh), patch('scripts.ci.codex_task.issue_body', return_value='running'), patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
                claim('owner/repo', candidate_report(), [], Path(root), 'run')
            values = dict(line.split('=', 1) for line in output.read_text().splitlines())
            self.assertEqual('true', values['claimed'])
            self.assertEqual('https://github.com/owner/repo/pull/7', values['existing_pr'])

    def test_public_issue_cannot_claim_the_build(self):
        issue = record()
        issue['user'] = {'login': 'stranger', 'type': 'User'}
        calls = []
        def fake_gh(*args):
            calls.append(args)
            if args[:2] == ('api', '--paginate'):
                return json.dumps([[issue]])
            if args[:2] == ('issue', 'create'):
                return 'https://github.com/owner/repo/issues/4'
            return ''
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'output'
            report = candidate_report()
            with patch('scripts.ci.codex_task.eligible_candidate', return_value=True), patch('scripts.ci.codex_task.candidate_key', return_value='3200-abc'), patch('scripts.ci.codex_task.issue_body', return_value='running'), patch('scripts.ci.codex_task.gh', side_effect=fake_gh), patch('scripts.ci.codex_task.issue_body', return_value='running'), patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
                claim('owner/repo', report, [], Path(root), 'run')
            self.assertIn('claimed=true', output.read_text())
            self.assertIn('issue=4', output.read_text())
            self.assertTrue(any(call[:2] == ('issue', 'create') and '--label' in call for call in calls))


if __name__ == '__main__':
    unittest.main()
