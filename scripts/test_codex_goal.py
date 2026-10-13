import tempfile
import json
import io
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from scripts.ci.codex_goal import run_goal, redact, write_diagnostics, AppServer


class FakeClient:
    def __init__(self, status='active'):
        self.calls = []
        self.status = status
        self.turn = 0
    def request(self, method, params):
        self.calls.append((method, params))
        if method == 'command/exec':
            return {'exitCode': 0, 'stdout': 'sandbox ready', 'stderr': ''}
        if method == 'thread/start':
            return {'thread': {'id': 'thread-1'}}
        if method == 'turn/start':
            self.turn += 1
            return {'turn': {'id': str(self.turn)}}
        if method == 'thread/goal/get':
            return {'goal': {'status': self.status, 'tokensUsed': self.turn * 100, 'tokenBudget': 10000}}
        if method == 'thread/read':
            return {'thread': {'turns': []}}
        return {}
    def wait_turn(self, thread, turn):
        return {'status': 'completed'}
    def pause(self, thread):
        self.request('thread/goal/set', {'threadId': thread, 'status': 'paused'})


class GoalTest(unittest.TestCase):
    def exercise(self, statuses, failures=()):
        client = FakeClient()
        states = iter(statuses)
        original = client.request
        def request(method, params):
            if method == 'thread/goal/get':
                client.calls.append((method, params))
                return {'goal': {'status': next(states)}}
            return original(method, params)
        client.request = request
        checkpoints = []
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.persist_turn', side_effect=lambda *args: checkpoints.append(args[2])), patch('scripts.ci.codex_goal.verify', side_effect=list(failures) or [None]) as verify:
            result = run_goal(client, Path(root), 'adapt')
        return result, client, checkpoints, verify.call_count

    def test_active_turn_does_not_run_independent_acceptance(self):
        result, client, checkpoints, count = self.exercise(['active', 'complete'])
        self.assertEqual('verified', result)
        self.assertEqual(1, count)
        self.assertEqual(3, len(checkpoints))
        self.assertFalse(any('tokenBudget' in params for _, params in client.calls))

    def test_rejected_completion_returns_feedback_to_same_thread(self):
        result, client, checkpoints, count = self.exercise(['complete', 'complete'], [ValueError('missing tablet UI'), None])
        self.assertEqual('verified', result)
        self.assertEqual(2, count)
        turns = [p for m,p in client.calls if m == 'turn/start']
        self.assertEqual(['thread-1', 'thread-1'], [p['threadId'] for p in turns])
        self.assertIn('missing tablet UI', turns[1]['input'][0]['text'])

    def test_blocked_goal_is_saved_without_independent_verification(self):
        result, client, checkpoints, count = self.exercise(['blocked'])
        self.assertEqual('blocked', result)
        self.assertEqual(1, len(checkpoints))
        self.assertEqual(0, count)

    def test_failed_checkpoint_prevents_next_turn(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.persist_turn', side_effect=RuntimeError('upload failed')), patch('scripts.ci.codex_goal.verify') as verify:
            with self.assertRaisesRegex(RuntimeError, 'upload failed'):
                run_goal(client, Path(root), 'adapt')
        self.assertEqual(1, client.turn)
        verify.assert_not_called()

    def test_checkpoint_handshake_waits_for_matching_durable_ack(self):
        from scripts.ci.codex_goal import persist_turn
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'CODEX_CHECKPOINT_HANDSHAKE':'true'}):
            directory = Path(root)
            client = FakeClient();client.thread_id = 'thread-1'
            (directory/'checkpoint-ack.json').write_text(json.dumps({'sequence':1, 'saved':True}))
            finished = threading.Event()
            worker = threading.Thread(target=lambda: (persist_turn(directory, client, 2), finished.set()))
            worker.start()
            try:
                time.sleep(0.1)
                self.assertFalse(finished.is_set())
                (directory/'checkpoint-ack.json').write_text(json.dumps({'sequence':2, 'saved':True}))
                worker.join(2)
                self.assertTrue(finished.is_set())
            finally:
                (directory/'checkpoint-ack.json').write_text(json.dumps({'sequence':2, 'saved':True}))
                worker.join(2)

    def test_rejected_upload_ack_fails_closed(self):
        from scripts.ci.codex_goal import persist_turn
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'CODEX_CHECKPOINT_HANDSHAKE':'true'}):
            directory = Path(root); client = FakeClient();client.thread_id = 'thread-1'
            (directory/'checkpoint-ack.json').write_text(json.dumps({'sequence':1, 'saved':False, 'error':'upload failed'}))
            with self.assertRaisesRegex(RuntimeError, 'upload failed'):
                persist_turn(directory, client, 1)

    def test_sandbox_failure_stops_before_creating_goal_or_spending_tokens(self):
        client = FakeClient()
        def request(method, params):
            client.calls.append((method, params))
            if method == 'command/exec':
                return {'exitCode': 1, 'stderr': 'bwrap: loopback: Operation not permitted'}
            self.fail('A failed sandbox must stop before creating a thread')
        client.request = request
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, 'Sandbox preflight failed'):
                run_goal(client, Path(root), 'adapt', 10000)
        self.assertEqual(0, client.turn)

    def test_failed_turn_is_not_verified_or_retried(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.persist_turn'), patch('scripts.ci.codex_goal.write_diagnostics'), patch('scripts.ci.codex_goal.verify') as verify, patch.object(client, 'wait_turn', return_value={'status': 'failed', 'error': {'message': 'tool unavailable'}}):
            result = run_goal(client, Path(root), 'adapt')
            verify.assert_not_called()
        self.assertEqual(1, client.turn)
        self.assertEqual('interrupted', result)

    def test_diagnostics_remain_valid_json_after_redaction(self):
        client = FakeClient()
        client.secrets = ['secret-value']
        client.messages = ['Bearer secret-value', 'access_token=secret-value']
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.auth_secrets', return_value=[]):
            write_diagnostics(Path(root), [{'error': 'secret-value'}], client)
            text = (Path(root) / 'goal-diagnostics.json').read_text()
            report = json.loads(text)
            self.assertNotIn('secret-value', text)
            self.assertEqual('[REDACTED]', report['iterations'][0]['error'])

    def test_nullable_tool_output_does_not_crash_event_reader(self):
        client = AppServer.__new__(AppServer)
        client.trace = MagicMock()
        client.secrets = []
        client.messages = []
        client.tool_output = {}
        client.process = MagicMock()
        client.process.stdout = io.StringIO(json.dumps({'method': 'item/completed', 'params': {'item': {'id': 'tool-1', 'type': 'commandExecution', 'exitCode': 1, 'aggregatedOutput': None}}}) + '\n')
        with patch('scripts.ci.codex_goal.auth_secrets', return_value=[]):
            client.read()
        self.assertEqual(1, len(client.messages))

    def test_known_credentials_and_jwt_are_redacted(self):
        jwt = 'eyJhbGciOiJIUzI1NiJ9.abcdefghijklmno.abcdefghijklmno'
        result = redact('token secret-value ' + jwt, ['secret-value'])
        self.assertNotIn('secret-value', result)
        self.assertNotIn(jwt, result)
