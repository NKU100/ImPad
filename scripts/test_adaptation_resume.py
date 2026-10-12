import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.ci import codex_task
from scripts.ci.codex_goal import run_goal
from scripts.test_codex_goal import FakeClient

class ResumeTest(unittest.TestCase):
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
