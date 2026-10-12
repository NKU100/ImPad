import tempfile
import unittest
import json
import os
import io
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from scripts.ci import codex_task
from scripts.ci.codex_goal import run_goal
from scripts.test_codex_goal import FakeClient

class ResumeTest(unittest.TestCase):
    def test_uncommented_issue_exposes_checkpoint_while_latest_apk_is_checked(self):
        digest = 'a' * 64
        issue = {
            'number': 11,
            'state': 'open',
            'created_at': '2026-10-10T00:00:00Z',
            'user': {'login': 'github-actions[bot]', 'type': 'Bot'},
            'labels': [{'name': 'impad-adaptation'}],
            'body': (f'<!-- impad-adaptation:wecom-com.tencent.wework-arm64-v8a-83835-{digest} -->\n'
                     'Status: `ADAPTATION_RUNNING`\n'
                     'Run: https://github.com/NKU100/ImPad/actions/runs/5678\n'
                     'Checkpoint run: https://github.com/NKU100/ImPad/actions/runs/5678\n'
                     'Checkpoint artifact: `impad-wecom-codex-adaptation-live-1-2`\n'),
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'github-output'
            with patch('scripts.ci.codex_task.gh', side_effect=[json.dumps([[issue]]), json.dumps([[]])]), \
                    patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}), \
                    redirect_stdout(io.StringIO()):
                result = codex_task.pending('NKU100/ImPad', 'wecom')

            self.assertEqual('false', result['pending_adaptation'])
            self.assertEqual('11', result['pending_issue'])
            self.assertEqual(f'wecom-com.tencent.wework-arm64-v8a-83835-{digest}',
                             result['pending_candidate_key'])
            self.assertEqual('5678', result['pending_checkpoint_run_id'])
            self.assertEqual('impad-wecom-codex-adaptation-live-1-2',
                             result['pending_checkpoint_artifact'])
            self.assertIn('pending_adaptation=false', output.read_text())

    def test_owner_comments_are_consumed_once_and_in_order(self):
        comments = [dict(id=i, body=b, user={'login':u,'type':'User'})
                    for i,b,u in [(4,'later','NKU100'),(2,'first','NKU100'),
                                  (3,'ignore','outsider'),(1,'old','NKU100'),(5,'  ','NKU100')]]
        self.assertEqual([2,4], [c['id'] for c in codex_task.owner_comments(comments,'NKU100',1)])

    def test_closed_tasks_do_not_resume(self):
        from scripts.test_codex_task import record
        issue=record();issue['state']='closed';issue['body']=issue['body'].replace('3200-abc','wecom-test')
        self.assertIsNone(codex_task.pending_attempt([issue],'wecom'))

    def test_native_resume_does_not_start_a_new_thread(self):
        client=FakeClient()
        original=client.request
        def request(method,params):
            if method=='thread/resume':
                client.calls.append((method,params))
                return {'thread':{'id':'saved-thread'}}
            return original(method,params)
        client.request=request
        with tempfile.TemporaryDirectory() as tmp, patch('scripts.ci.codex_goal.verify'), patch('scripts.ci.codex_goal.write_diagnostics'):
            run_goal(client,Path(tmp),'adapt',{'nativeThreadId':'saved-thread','ownerComments':[{'body':'fix'}]})
        self.assertFalse(any(m=='thread/start' for m,p in client.calls))
        self.assertTrue(any(m=='thread/resume' for m,p in client.calls))
        feedback=next(p['input'][0]['text'] for m,p in client.calls if m=='turn/start')
        self.assertIn('Prioritize these new instructions from the repository owner',feedback)
