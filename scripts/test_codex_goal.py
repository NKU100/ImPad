import tempfile
import json
import io
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
    def test_running_goal_publishes_thread_and_delivered_input_for_live_checkpoint(self):
        client = FakeClient()
        snapshots = []
        def capture(directory, iterations, client, status='continue'):
            snapshots.append({'status': status, 'threadId': client.thread_id,
                              'inputDelivered': getattr(client, 'input_delivered', False)})
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.write_diagnostics', side_effect=capture), \
                patch('scripts.ci.codex_goal.verify', side_effect=ValueError('pending')):
            run_goal(client, Path(root), 'adapt')
        self.assertIn({'status':'running','threadId':'thread-1','inputDelivered':False}, snapshots)
        self.assertIn({'status':'running','threadId':'thread-1','inputDelivered':True}, snapshots)

    def test_adaptation_step_uses_no_token_budget_and_stops_after_one_turn(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.write_diagnostics'):
            with patch('scripts.ci.codex_goal.verify', side_effect=ValueError('missing profile')) as verify:
                result = run_goal(client, Path(root), 'adapt', resume_context={'previousStage': 'adaptation-in-progress'})

        goal = next(params for method, params in client.calls
                    if method == 'thread/goal/set' and 'objective' in params)
        turn = next(params for method, params in client.calls if method == 'turn/start')
        self.assertNotIn('tokenBudget', goal)
        self.assertEqual(1, client.turn)
        self.assertEqual(1, verify.call_count)
        self.assertEqual('continue', result)
        self.assertIn('previousStage', turn['input'][0]['text'])

    def test_step_reports_verified_only_after_independent_validation(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.write_diagnostics'), \
                patch('scripts.ci.codex_goal.verify') as verify:
            result = run_goal(client, Path(root), 'adapt')

        self.assertEqual('verified', result)
        self.assertEqual(1, client.turn)
        self.assertTrue(any(method == 'thread/goal/set' and params.get('status') == 'complete'
                            for method, params in client.calls))

    def test_long_instructions_remain_in_first_continuation_turn(self):
        client = FakeClient()
        prompt = 'Full constraints ' * 500
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.write_diagnostics'), patch('scripts.ci.codex_goal.verify'):
            run_goal(client, Path(root), prompt)
        turn = next(p for m, p in client.calls if m == 'turn/start')
        goal = next(p for m, p in client.calls if m == 'thread/goal/set' and 'objective' in p)
        self.assertNotIn('tokenBudget', goal)
        self.assertTrue(turn['input'][0]['text'].startswith(prompt))

    def test_failed_validation_is_checkpointed_for_the_next_thread(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.write_diagnostics'):
            with patch('scripts.ci.codex_goal.verify', side_effect=[ValueError('missing profile'), None]) as verify:
                result = run_goal(client, Path(root), 'adapt', {'previousStage': 'adaptation-in-progress'})
            self.assertEqual(1, verify.call_count)
        turns = [p for m,p in client.calls if m == 'turn/start']
        self.assertEqual(['thread-1'], [p['threadId'] for p in turns])
        self.assertIn('previousStage', turns[0]['input'][0]['text'])
        self.assertEqual('continue', result)

    def test_model_completion_does_not_bypass_independent_validation(self):
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.write_diagnostics'), patch('scripts.ci.codex_goal.verify', side_effect=ValueError('missing profile')):
            result = run_goal(FakeClient('complete'), Path(root), 'adapt')
        self.assertEqual('continue', result)

    def test_runtime_failure_does_not_end_automatic_continuation(self):
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.write_diagnostics'), \
                patch('scripts.ci.codex_goal.verify', side_effect=ValueError('No tablet entry')):
            client = FakeClient()
            result = run_goal(client, Path(root), 'adapt with AVD')
        self.assertEqual('continue', result)
        self.assertEqual(1, client.turn)

    def test_no_goal_token_budget_is_sent_to_codex(self):
        client = FakeClient('budgetLimited')
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.write_diagnostics'), patch('scripts.ci.codex_goal.verify', side_effect=ValueError('missing profile')):
            result = run_goal(client, Path(root), 'adapt')
        self.assertEqual(1, client.turn)
        self.assertEqual('continue', result)
        self.assertFalse(any('tokenBudget' in params for method, params in client.calls))

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
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.write_diagnostics'), patch('scripts.ci.codex_goal.verify') as verify, patch.object(client, 'wait_turn', return_value={'status': 'failed', 'error': {'message': 'tool unavailable'}}):
            result = run_goal(client, Path(root), 'adapt')
            verify.assert_not_called()
        self.assertEqual(1, client.turn)
        self.assertEqual('continue', result)

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
